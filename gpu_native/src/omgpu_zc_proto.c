/*
 * omgpu_zc_proto: zero-copy transcode prototype.
 *   MediaCodec HW decode -> AImageReader (AHardwareBuffer, GPU-sampled)
 *   -> EGLImage / GL_TEXTURE_EXTERNAL_OES -> GLES draw (fit/pad transform)
 *   -> encoder input Surface -> MediaCodec HW encode -> AMediaMuxer.
 * No pixel data crosses the CPU. Used to measure the ceiling of the design.
 *
 * usage: omgpu_zc_proto <in.mp4> <out.mp4> <outW> <outH> <cq>
 */
#include <android/hardware_buffer.h>
#include <android/native_window.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <media/NdkImage.h>
#include <media/NdkImageReader.h>
#include <media/NdkMediaCodec.h>
#include <media/NdkMediaExtractor.h>
#include <media/NdkMediaFormat.h>
#include <media/NdkMediaMuxer.h>

#define EGL_EGLEXT_PROTOTYPES
#define GL_GLEXT_PROTOTYPES
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES3/gl3.h>
#include <GLES2/gl2ext.h>
#include <math.h>

static double now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec * 1e-9; }
#define DIE(...) do { fprintf(stderr, __VA_ARGS__); fputc('\n', stderr); _exit(2); } while (0)

static void start_binder_pool(void) {
    void *h = dlopen("libbinder_ndk.so", RTLD_NOW);
    bool (*set_max)(uint32_t) = h ? (bool (*)(uint32_t))dlsym(h, "ABinderProcess_setThreadPoolMaxThreadCount") : NULL;
    void (*start)(void) = h ? (void (*)(void))dlsym(h, "ABinderProcess_startThreadPool") : NULL;
    if (set_max) set_max(2);
    if (start) start();
}

static GLuint compile(GLenum type, const char *src) {
    GLuint s = glCreateShader(type);
    glShaderSource(s, 1, &src, NULL);
    glCompileShader(s);
    GLint ok; glGetShaderiv(s, GL_COMPILE_STATUS, &ok);
    if (!ok) { char log[1024]; glGetShaderInfoLog(s, sizeof log, NULL, log); DIE("shader: %s", log); }
    return s;
}

static const char *VS =
    "#version 300 es\n"
    "layout(location=0) in vec2 pos;\n"
    "uniform vec4 rect;\n" /* x0,y0,x1,y1 in NDC */
    "out vec2 uv;\n"
    "void main(){ vec2 p = mix(rect.xy, rect.zw, pos); gl_Position = vec4(p,0.0,1.0); uv = vec2(pos.x, 1.0-pos.y); }\n";
static const char *FS =
    "#version 300 es\n"
    "#extension GL_OES_EGL_image_external_essl3 : require\n"
    "precision highp float;\n"
    "uniform samplerExternalOES tex;\n"
    "in vec2 uv; out vec4 o;\n"
    "void main(){ o = texture(tex, uv); }\n";

/* Frames handed from the ImageReader listener thread. */
static pthread_mutex_t mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t cv = PTHREAD_COND_INITIALIZER;
static int images_ready = 0;
static void on_image(void *ctx, AImageReader *r) { (void)ctx; (void)r; pthread_mutex_lock(&mu); images_ready++; pthread_cond_signal(&cv); pthread_mutex_unlock(&mu); }

static AMediaCodec *g_enc;
static AMediaMuxer *g_mux;
static long g_frames_out;
static void *drain_encoder(void *arg) {
    (void)arg;
    ssize_t track = -1;
    for (;;) {
        AMediaCodecBufferInfo info;
        ssize_t ob = AMediaCodec_dequeueOutputBuffer(g_enc, &info, 20000);
        if (ob == AMEDIACODEC_INFO_OUTPUT_FORMAT_CHANGED) {
            track = AMediaMuxer_addTrack(g_mux, AMediaCodec_getOutputFormat(g_enc));
            AMediaMuxer_start(g_mux);
        } else if (ob >= 0) {
            size_t sz; uint8_t *d = AMediaCodec_getOutputBuffer(g_enc, ob, &sz);
            if (info.size > 0 && track >= 0 && !(info.flags & AMEDIACODEC_BUFFER_FLAG_CODEC_CONFIG)) {
                AMediaMuxer_writeSampleData(g_mux, track, d + info.offset, &info);
                g_frames_out++;
            }
            AMediaCodec_releaseOutputBuffer(g_enc, ob, false);
            if (info.flags & AMEDIACODEC_BUFFER_FLAG_END_OF_STREAM) return NULL;
        }
    }
}

int main(int argc, char **argv) {
    if (argc < 6) DIE("usage: %s in out W H cq", argv[0]);
    const char *in = argv[1], *out = argv[2];
    int W = atoi(argv[3]), H = atoi(argv[4]), cq = atoi(argv[5]);
    start_binder_pool();

    int ifd = open(in, O_RDONLY);
    if (ifd < 0) DIE("open %s", in);
    AMediaExtractor *ex = AMediaExtractor_new();
    if (AMediaExtractor_setDataSourceFd(ex, ifd, 0, lseek(ifd, 0, SEEK_END)) != AMEDIA_OK) DIE("extractor");
    int vtrack = -1; AMediaFormat *ifmt = NULL; const char *mime = NULL;
    for (size_t i = 0; i < AMediaExtractor_getTrackCount(ex); i++) {
        AMediaFormat *f = AMediaExtractor_getTrackFormat(ex, i);
        const char *m; AMediaFormat_getString(f, AMEDIAFORMAT_KEY_MIME, &m);
        if (!strncmp(m, "video/", 6)) { vtrack = (int)i; ifmt = f; mime = m; break; }
        AMediaFormat_delete(f);
    }
    if (vtrack < 0) DIE("no video track");
    AMediaExtractor_selectTrack(ex, vtrack);
    int32_t iw, ih; AMediaFormat_getInt32(ifmt, AMEDIAFORMAT_KEY_WIDTH, &iw); AMediaFormat_getInt32(ifmt, AMEDIAFORMAT_KEY_HEIGHT, &ih);

    /* Encoder with input surface. */
    AMediaCodec *enc = AMediaCodec_createEncoderByType("video/avc");
    AMediaFormat *ef = AMediaFormat_new();
    AMediaFormat_setString(ef, AMEDIAFORMAT_KEY_MIME, "video/avc");
    AMediaFormat_setInt32(ef, AMEDIAFORMAT_KEY_WIDTH, W);
    AMediaFormat_setInt32(ef, AMEDIAFORMAT_KEY_HEIGHT, H);
    AMediaFormat_setInt32(ef, AMEDIAFORMAT_KEY_COLOR_FORMAT, 0x7F000789); /* COLOR_FormatSurface */
    AMediaFormat_setInt32(ef, AMEDIAFORMAT_KEY_FRAME_RATE, 30);
    AMediaFormat_setInt32(ef, AMEDIAFORMAT_KEY_I_FRAME_INTERVAL, 8);
    AMediaFormat_setInt32(ef, "bitrate-mode", 0); /* CQ */
    AMediaFormat_setInt32(ef, "quality", cq);
    AMediaFormat_setInt32(ef, AMEDIAFORMAT_KEY_BIT_RATE, 20000000);
    AMediaFormat_setInt32(ef, AMEDIAFORMAT_KEY_PROFILE, 8); /* AVCProfileHigh */
    AMediaFormat_setInt32(ef, AMEDIAFORMAT_KEY_LEVEL, 0x200); /* 4.1 */
    /* Transcoding, not playback: ask the codecs to run as fast as they can. */
    int oprate = getenv("ZC_OPRATE") ? atoi(getenv("ZC_OPRATE")) : 32767;
    int prio = getenv("ZC_PRIO") ? atoi(getenv("ZC_PRIO")) : 0;
    if (oprate > 0) AMediaFormat_setInt32(ef, "operating-rate", oprate);
    if (prio >= 0) AMediaFormat_setInt32(ef, "priority", prio);
    if (AMediaCodec_configure(enc, ef, NULL, NULL, AMEDIACODEC_CONFIGURE_FLAG_ENCODE) != AMEDIA_OK) DIE("enc configure");
    ANativeWindow *encWin = NULL;
    if (AMediaCodec_createInputSurface(enc, &encWin) != AMEDIA_OK) DIE("createInputSurface");
    AMediaCodec_start(enc);

    /* EGL on the encoder surface. */
    EGLDisplay dpy = eglGetDisplay(EGL_DEFAULT_DISPLAY);
    eglInitialize(dpy, NULL, NULL);
    const EGLint cattr[] = {EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_RENDERABLE_TYPE, EGL_OPENGL_ES3_BIT,
                            EGL_RECORDABLE_ANDROID, 1, EGL_NONE};
    EGLConfig cfg; EGLint nc;
    if (!eglChooseConfig(dpy, cattr, &cfg, 1, &nc) || nc < 1) DIE("eglChooseConfig");
    const EGLint xattr[] = {EGL_CONTEXT_CLIENT_VERSION, 3, EGL_NONE};
    EGLContext ctx = eglCreateContext(dpy, cfg, EGL_NO_CONTEXT, xattr);
    /* Termux headers type EGLNativeWindowType as X11 unsigned long; Android EGL
     * takes the ANativeWindow pointer (same width on arm64). */
    EGLSurface surf = eglCreateWindowSurface(dpy, cfg, (EGLNativeWindowType)encWin, NULL);
    if (surf == EGL_NO_SURFACE) DIE("eglCreateWindowSurface 0x%x", eglGetError());
    eglMakeCurrent(dpy, surf, surf, ctx);
    fprintf(stderr, "GL_RENDERER=%s\n", glGetString(GL_RENDERER));
    GLuint prog = glCreateProgram();
    glAttachShader(prog, compile(GL_VERTEX_SHADER, VS));
    glAttachShader(prog, compile(GL_FRAGMENT_SHADER, FS));
    glLinkProgram(prog);
    glUseProgram(prog);
    static const float quad[] = {0, 0, 1, 0, 0, 1, 1, 1};
    GLuint vbo; glGenBuffers(1, &vbo); glBindBuffer(GL_ARRAY_BUFFER, vbo);
    glBufferData(GL_ARRAY_BUFFER, sizeof quad, quad, GL_STATIC_DRAW);
    glEnableVertexAttribArray(0); glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, 0);
    GLuint tex; glGenTextures(1, &tex);
    glBindTexture(GL_TEXTURE_EXTERNAL_OES, tex);
    glTexParameteri(GL_TEXTURE_EXTERNAL_OES, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_EXTERNAL_OES, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    /* Fit (letterbox) rectangle in NDC. */
    double s = fmin((double)W / iw, (double)H / ih);
    double dw = iw * s, dh = ih * s;
    float rx0 = (float)(-dw / W), rx1 = (float)(dw / W), ry0 = (float)(-dh / H), ry1 = (float)(dh / H);
    glUniform4f(glGetUniformLocation(prog, "rect"), rx0, ry0, rx1, ry1);
    glViewport(0, 0, W, H);

    /* Decoder -> ImageReader. */
    AImageReader *reader;
    if (AImageReader_newWithUsage(iw, ih, AIMAGE_FORMAT_PRIVATE, AHARDWAREBUFFER_USAGE_GPU_SAMPLED_IMAGE, 4, &reader) != AMEDIA_OK)
        DIE("AImageReader_newWithUsage");
    AImageReader_ImageListener lis = {NULL, on_image};
    AImageReader_setImageListener(reader, &lis);
    ANativeWindow *decWin; AImageReader_getWindow(reader, &decWin);
    AMediaCodec *dec = AMediaCodec_createDecoderByType(mime);
    if (oprate > 0) AMediaFormat_setInt32(ifmt, "operating-rate", oprate);
    if (prio >= 0) AMediaFormat_setInt32(ifmt, "priority", prio);
    if (AMediaCodec_configure(dec, ifmt, decWin, NULL, 0) != AMEDIA_OK) DIE("dec configure");
    AMediaCodec_start(dec);

    int ofd = open(out, O_CREAT | O_TRUNC | O_RDWR, 0644);
    g_mux = AMediaMuxer_new(ofd, AMEDIAMUXER_OUTPUT_FORMAT_MPEG_4);
    g_enc = enc;
    pthread_t drain_thr;
    pthread_create(&drain_thr, NULL, drain_encoder, NULL);
    bool in_eos = false, dec_eos = false;
    long frames_in = 0, frames_drawn = 0, released = 0;
    /* EGLImage cache: ImageReader cycles a small fixed buffer set. */
    struct { AHardwareBuffer *hb; EGLImageKHR img; } cache[16]; int ncache = 0;
    double t0 = now();
    double last_dbg = now();
    while (!dec_eos || frames_drawn < released) {
        if (getenv("ZC_DBG") && now() - last_dbg > 1.0) {
            last_dbg = now();
            fprintf(stderr, "dbg in=%ld released=%ld drawn=%ld ready=%d out=%ld in_eos=%d dec_eos=%d\n",
                    frames_in, released, frames_drawn, images_ready, g_frames_out, in_eos, dec_eos);
        }
        if (!in_eos) {
            ssize_t ib = AMediaCodec_dequeueInputBuffer(dec, 0);
            if (ib >= 0) {
                size_t cap; uint8_t *buf = AMediaCodec_getInputBuffer(dec, ib, &cap);
                ssize_t n = AMediaExtractor_readSampleData(ex, buf, cap);
                if (n < 0) { AMediaCodec_queueInputBuffer(dec, ib, 0, 0, 0, AMEDIACODEC_BUFFER_FLAG_END_OF_STREAM); in_eos = true; }
                else { AMediaCodec_queueInputBuffer(dec, ib, 0, n, AMediaExtractor_getSampleTime(ex), 0); AMediaExtractor_advance(ex); frames_in++; }
            }
        }
        if (!dec_eos && released - frames_drawn < 3) {
            AMediaCodecBufferInfo info;
            ssize_t ob = AMediaCodec_dequeueOutputBuffer(dec, &info, 1000);
            if (ob >= 0) {
                bool render = info.size > 0;
                AMediaCodec_releaseOutputBuffer(dec, ob, render);
                if (render) released++;
                if (info.flags & AMEDIACODEC_BUFFER_FLAG_END_OF_STREAM) dec_eos = true;
            }
        }
        /* Draw every image that has arrived. */
        for (;;) {
            pthread_mutex_lock(&mu);
            int ready = images_ready;
            if (ready == 0 && (released > frames_drawn) && (dec_eos || released - frames_drawn >= 3)) {
                struct timespec dl; clock_gettime(CLOCK_REALTIME, &dl); dl.tv_nsec += 50000000;
                if (dl.tv_nsec >= 1000000000) { dl.tv_sec++; dl.tv_nsec -= 1000000000; }
                pthread_cond_timedwait(&cv, &mu, &dl); ready = images_ready;
            }
            if (ready > 0) images_ready--;
            pthread_mutex_unlock(&mu);
            if (ready == 0) break;
            AImage *img;
            if (AImageReader_acquireNextImage(reader, &img) != AMEDIA_OK) break;
            int64_t ts; AImage_getTimestamp(img, &ts);
            AHardwareBuffer *hb; AImage_getHardwareBuffer(img, &hb);
            EGLImageKHR ei = EGL_NO_IMAGE_KHR;
            for (int i = 0; i < ncache; i++) if (cache[i].hb == hb) ei = cache[i].img;
            if (ei == EGL_NO_IMAGE_KHR) {
                const EGLint ia[] = {EGL_IMAGE_PRESERVED_KHR, EGL_TRUE, EGL_NONE};
                ei = eglCreateImageKHR(dpy, EGL_NO_CONTEXT, EGL_NATIVE_BUFFER_ANDROID, eglGetNativeClientBufferANDROID(hb), ia);
                if (ncache < 16) { cache[ncache].hb = hb; cache[ncache].img = ei; ncache++; }
            }
            glEGLImageTargetTexture2DOES(GL_TEXTURE_EXTERNAL_OES, (GLeglImageOES)ei);
            glClear(GL_COLOR_BUFFER_BIT);
            glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
            eglPresentationTimeANDROID(dpy, surf, (EGLnsecsANDROID)ts);
            eglSwapBuffers(dpy, surf);
            AImage_delete(img);
            frames_drawn++;
        }
    }
    AMediaCodec_signalEndOfInputStream(enc);
    pthread_join(drain_thr, NULL);
    long frames_out = g_frames_out;
    double t1 = now();
    AMediaMuxer_stop(g_mux); AMediaMuxer_delete(g_mux); close(ofd);
    fprintf(stderr, "in=%ld drawn=%ld out=%ld wall=%.3fs fps=%.1f\n", frames_in, frames_drawn, frames_out, t1 - t0, frames_out / (t1 - t0));
    fflush(stderr);
    _exit(0);
}
