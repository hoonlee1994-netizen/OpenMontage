/*
 * omgpu-probe: report platform-attested GPU / media-hardware evidence as JSON.
 *
 *  - MediaCodec: for each video MIME type, the codec the platform hands out
 *    for encode/decode, and its AMediaCodecInfo type (SOFTWARE_ONLY vs
 *    HARDWARE_ACCELERATED, Android 16 / API 36), vendor flag and max instances.
 *  - Vulkan: physical devices from the Android system loader (the same one
 *    FFmpeg and our engines use), with device type and driver identity.
 *    CPU-type devices (llvmpipe/lavapipe/SwiftShader) are reported as such.
 *  - EGL/GLES: GL_VENDOR / GL_RENDERER / GL_VERSION from the system EGL.
 *  - /dev/mali0: whether this process holds an fd to the Mali kernel driver
 *    after creating GPU contexts (evidence the vendor driver, not a software
 *    rasteriser, is in use).
 */
#include <dlfcn.h>
#include <dirent.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <media/NdkMediaCodec.h>
#include <media/NdkMediaCodecInfo.h>
#include <media/NdkMediaFormat.h>

#define VK_NO_PROTOTYPES
#include <vulkan/vulkan.h>

#include <EGL/egl.h>
#include <GLES3/gl3.h>

static void json_str(const char *s) {
    putchar('"');
    for (; s && *s; s++) {
        if (*s == '"' || *s == '\\') { putchar('\\'); putchar(*s); }
        else if ((unsigned char)*s < 0x20) printf("\\u%04x", *s);
        else putchar(*s);
    }
    putchar('"');
}

static const char *codec_type_name(int t) {
    switch (t) {
    case 1: return "SOFTWARE_ONLY";
    case 2: return "HARDWARE_ACCELERATED";
    case 3: return "SOFTWARE_WITH_DEVICE_ACCESS";
    default: return "INVALID";
    }
}

static void start_binder_pool(void) {
    void *h = dlopen("libbinder_ndk.so", RTLD_NOW);
    if (!h) return;
    bool (*set_max)(uint32_t) = (bool (*)(uint32_t))dlsym(h, "ABinderProcess_setThreadPoolMaxThreadCount");
    void (*start)(void) = (void (*)(void))dlsym(h, "ABinderProcess_startThreadPool");
    if (set_max) set_max(1);
    if (start) start();
}

static void probe_codec(const char *mime, bool encoder, bool *first) {
    AMediaCodec *c = encoder ? AMediaCodec_createEncoderByType(mime) : AMediaCodec_createDecoderByType(mime);
    if (!*first) printf(",");
    *first = false;
    printf("{\"mime\":"); json_str(mime);
    printf(",\"role\":\"%s\"", encoder ? "encoder" : "decoder");
    if (!c) { printf(",\"available\":false}"); return; }
    char *name = NULL;
    AMediaCodec_getName(c, &name);
    printf(",\"available\":true,\"name\":"); json_str(name);
    /* API 36 codec-info calls are resolved at runtime so the probe still
     * builds against older NDK API levels and degrades gracefully. */
    static void *mndk;
    if (!mndk) mndk = dlopen("libmediandk.so", RTLD_NOW);
    media_status_t (*get_info)(const char *, const AMediaCodecInfo **) =
        mndk ? dlsym(mndk, "AMediaCodecStore_getCodecInfo") : NULL;
    int32_t (*info_type)(const AMediaCodecInfo *) = mndk ? dlsym(mndk, "AMediaCodecInfo_getMediaCodecInfoType") : NULL;
    int32_t (*is_vendor)(const AMediaCodecInfo *) = mndk ? dlsym(mndk, "AMediaCodecInfo_isVendor") : NULL;
    int32_t (*max_inst)(const AMediaCodecInfo *) = mndk ? dlsym(mndk, "AMediaCodecInfo_getMaxSupportedInstances") : NULL;
    const char *(*canon)(const AMediaCodecInfo *) = mndk ? dlsym(mndk, "AMediaCodecInfo_getCanonicalName") : NULL;
    const AMediaCodecInfo *info = NULL;
    if (name && get_info && get_info(name, &info) == AMEDIA_OK && info) {
        if (info_type) printf(",\"type\":\"%s\"", codec_type_name(info_type(info)));
        if (is_vendor) printf(",\"vendor\":%s", is_vendor(info) ? "true" : "false");
        if (max_inst) printf(",\"max_instances\":%d", max_inst(info));
        if (canon) { printf(",\"canonical_name\":"); json_str(canon(info)); }
    } else {
        printf(",\"type\":\"UNKNOWN\"");
    }
    printf("}");
    if (name) AMediaCodec_releaseName(c, name);
    AMediaCodec_delete(c);
}

static bool fd_points_to(const char *target) {
    DIR *d = opendir("/proc/self/fd");
    if (!d) return false;
    struct dirent *e;
    char path[64], link[256];
    bool found = false;
    while ((e = readdir(d))) {
        snprintf(path, sizeof path, "/proc/self/fd/%s", e->d_name);
        ssize_t n = readlink(path, link, sizeof link - 1);
        if (n > 0) { link[n] = 0; if (strcmp(link, target) == 0) found = true; }
    }
    closedir(d);
    return found;
}

static bool maps_contains(const char *needle) {
    FILE *f = fopen("/proc/self/maps", "r");
    if (!f) return false;
    char line[512];
    bool found = false;
    while (fgets(line, sizeof line, f)) if (strstr(line, needle)) { found = true; break; }
    fclose(f);
    return found;
}

static void probe_vulkan(void) {
    printf("\"vulkan\":{");
    void *lib = dlopen("libvulkan.so", RTLD_NOW);
    if (!lib) { printf("\"available\":false,\"error\":"); json_str(dlerror()); printf("}"); return; }
    PFN_vkGetInstanceProcAddr gipa = (PFN_vkGetInstanceProcAddr)dlsym(lib, "vkGetInstanceProcAddr");
    PFN_vkCreateInstance createInstance = (PFN_vkCreateInstance)gipa(NULL, "vkCreateInstance");
    VkApplicationInfo app = {VK_STRUCTURE_TYPE_APPLICATION_INFO, NULL, "omgpu-probe", 1, "omgpu", 1, VK_API_VERSION_1_1};
    VkInstanceCreateInfo ici = {VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, NULL, 0, &app, 0, NULL, 0, NULL};
    VkInstance inst;
    VkResult r = createInstance(&ici, NULL, &inst);
    if (r != VK_SUCCESS) { printf("\"available\":false,\"vkCreateInstance\":%d}", r); return; }
    PFN_vkEnumeratePhysicalDevices enumDev = (PFN_vkEnumeratePhysicalDevices)gipa(inst, "vkEnumeratePhysicalDevices");
    PFN_vkGetPhysicalDeviceProperties2 props2 = (PFN_vkGetPhysicalDeviceProperties2)gipa(inst, "vkGetPhysicalDeviceProperties2");
    PFN_vkGetPhysicalDeviceQueueFamilyProperties qfp = (PFN_vkGetPhysicalDeviceQueueFamilyProperties)gipa(inst, "vkGetPhysicalDeviceQueueFamilyProperties");
    PFN_vkCreateDevice createDevice = (PFN_vkCreateDevice)gipa(inst, "vkCreateDevice");
    PFN_vkDestroyDevice destroyDevice = (PFN_vkDestroyDevice)gipa(inst, "vkDestroyDevice");
    PFN_vkDestroyInstance destroyInstance = (PFN_vkDestroyInstance)gipa(inst, "vkDestroyInstance");
    uint32_t n = 0;
    enumDev(inst, &n, NULL);
    VkPhysicalDevice devs[8];
    if (n > 8) n = 8;
    enumDev(inst, &n, devs);
    printf("\"available\":true,\"devices\":[");
    int gpu_index = -1;
    for (uint32_t i = 0; i < n; i++) {
        VkPhysicalDeviceDriverProperties drv = {VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DRIVER_PROPERTIES};
        VkPhysicalDeviceProperties2 p = {VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2, &drv};
        props2(devs[i], &p);
        const VkPhysicalDeviceProperties *pp = &p.properties;
        if (i) printf(",");
        printf("{\"name\":"); json_str(pp->deviceName);
        printf(",\"vendor_id\":%u,\"device_id\":%u,\"type\":%d,\"api\":\"%u.%u.%u\"",
               pp->vendorID, pp->deviceID, pp->deviceType,
               VK_API_VERSION_MAJOR(pp->apiVersion), VK_API_VERSION_MINOR(pp->apiVersion), VK_API_VERSION_PATCH(pp->apiVersion));
        printf(",\"driver_id\":%d,\"driver_name\":", drv.driverID); json_str(drv.driverName);
        printf(",\"driver_info\":"); json_str(drv.driverInfo);
        printf(",\"timestamp_period_ns\":%f}", pp->limits.timestampPeriod);
        if (gpu_index < 0 && pp->deviceType != VK_PHYSICAL_DEVICE_TYPE_CPU) gpu_index = (int)i;
    }
    printf("]");
    if (gpu_index >= 0) {
        float prio = 1.0f;
        uint32_t qn = 0;
        qfp(devs[gpu_index], &qn, NULL);
        VkDeviceQueueCreateInfo q = {VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO, NULL, 0, 0, 1, &prio};
        VkDeviceCreateInfo dci = {VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO, NULL, 0, 1, &q, 0, NULL, 0, NULL, NULL};
        VkDevice dev;
        bool ok = qn && createDevice(devs[gpu_index], &dci, NULL, &dev) == VK_SUCCESS;
        printf(",\"device_created\":%s,\"dev_mali0_fd\":%s,\"vendor_driver_mapped\":%s",
               ok ? "true" : "false",
               fd_points_to("/dev/mali0") ? "true" : "false",
               maps_contains("vulkan.mali.so") ? "true" : "false");
        if (ok) destroyDevice(dev, NULL);
    }
    printf("}");
    destroyInstance(inst, NULL);
}

static void probe_egl(void) {
    printf("\"egl\":{");
    void *egl = dlopen("libEGL.so", RTLD_NOW);
    void *gles = dlopen("libGLESv3.so", RTLD_NOW);
    if (!egl || !gles) { printf("\"available\":false}"); return; }
    EGLDisplay (*getDisplay)(EGLNativeDisplayType) = dlsym(egl, "eglGetDisplay");
    EGLBoolean (*init)(EGLDisplay, EGLint *, EGLint *) = dlsym(egl, "eglInitialize");
    EGLBoolean (*choose)(EGLDisplay, const EGLint *, EGLConfig *, EGLint, EGLint *) = dlsym(egl, "eglChooseConfig");
    EGLContext (*createCtx)(EGLDisplay, EGLConfig, EGLContext, const EGLint *) = dlsym(egl, "eglCreateContext");
    EGLSurface (*createPb)(EGLDisplay, EGLConfig, const EGLint *) = dlsym(egl, "eglCreatePbufferSurface");
    EGLBoolean (*makeCurrent)(EGLDisplay, EGLSurface, EGLSurface, EGLContext) = dlsym(egl, "eglMakeCurrent");
    EGLBoolean (*terminate)(EGLDisplay) = dlsym(egl, "eglTerminate");
    const GLubyte *(*getString)(GLenum) = dlsym(gles, "glGetString");
    EGLDisplay d = getDisplay(EGL_DEFAULT_DISPLAY);
    EGLint maj = 0, min = 0;
    if (!init(d, &maj, &min)) { printf("\"available\":false,\"error\":\"eglInitialize\"}"); return; }
    const EGLint cfg_attr[] = {EGL_RENDERABLE_TYPE, 0x40 /*EGL_OPENGL_ES3_BIT*/, EGL_SURFACE_TYPE, EGL_PBUFFER_BIT,
                               EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_NONE};
    EGLConfig cfg; EGLint nc = 0;
    choose(d, cfg_attr, &cfg, 1, &nc);
    const EGLint ctx_attr[] = {0x3098 /*EGL_CONTEXT_CLIENT_VERSION*/, 3, EGL_NONE};
    EGLContext ctx = createCtx(d, cfg, EGL_NO_CONTEXT, ctx_attr);
    const EGLint pb_attr[] = {EGL_WIDTH, 16, EGL_HEIGHT, 16, EGL_NONE};
    EGLSurface s = createPb(d, cfg, pb_attr);
    makeCurrent(d, s, s, ctx);
    printf("\"available\":true,\"egl_version\":\"%d.%d\",\"gl_vendor\":", maj, min);
    json_str((const char *)getString(GL_VENDOR));
    printf(",\"gl_renderer\":"); json_str((const char *)getString(GL_RENDERER));
    printf(",\"gl_version\":"); json_str((const char *)getString(GL_VERSION));
    printf(",\"dev_mali0_fd\":%s,\"vendor_driver_mapped\":%s}",
           fd_points_to("/dev/mali0") ? "true" : "false", maps_contains("libGLES_mali.so") ? "true" : "false");
    terminate(d);
}

int main(int argc, char **argv) {
    bool want_codecs = true, want_vk = true, want_egl = true;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--no-codecs")) want_codecs = false;
        if (!strcmp(argv[i], "--no-vulkan")) want_vk = false;
        if (!strcmp(argv[i], "--no-egl")) want_egl = false;
    }
    printf("{\"probe\":\"omgpu-probe\",\"version\":1");
    if (want_codecs) {
        start_binder_pool();
        static const char *mimes[] = {"video/avc", "video/hevc", "video/x-vnd.on2.vp9", "video/av01", NULL};
        printf(",\"mediacodec\":[");
        bool first = true;
        for (int i = 0; mimes[i]; i++) {
            probe_codec(mimes[i], true, &first);
            probe_codec(mimes[i], false, &first);
        }
        printf("]");
    }
    if (want_vk) { printf(","); probe_vulkan(); }
    if (want_egl) { printf(","); probe_egl(); }
    printf("}\n");
    fflush(stdout);
    /* Skip static destructors: tearing down the vendor GL/Vulkan driver and
     * binder threads at exit() can abort() after the report is complete. */
    _exit(0);
}
