/*
 * om_binder_pool: start the binder thread pool before any codec code runs.
 *
 * Android Codec2 (MediaCodec) delivers buffer/work-done callbacks to the
 * client over binder, so a plain native executable must run a binder thread
 * pool or the codec stalls after start().
 *
 * FFmpeg 8.x (libavutil android binder helper) calls
 * ABinderProcess_setThreadPoolMaxThreadCount(1) + startThreadPool() lazily,
 * but a MediaCodec decoder configure() can start the pool first (from
 * libaudioclient, at libbinder's default size of 15); FFmpeg's later resize
 * to 1 is then a "shrink after start", which libbinder turns into abort().
 * Starting the pool here, at the size FFmpeg itself requests, makes every
 * later call a no-op. Override with OM_BINDER_POOL_THREADS.
 */
#include <dlfcn.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>

__attribute__((constructor)) static void om_start_binder_pool(void) {
    if (getenv("OM_BINDER_POOL_DISABLE")) return;
    void *h = dlopen("libbinder_ndk.so", RTLD_NOW);
    if (!h) return;
    bool (*set_max)(uint32_t) = (bool (*)(uint32_t))dlsym(h, "ABinderProcess_setThreadPoolMaxThreadCount");
    void (*start)(void) = (void (*)(void))dlsym(h, "ABinderProcess_startThreadPool");
    uint32_t n = 1;
    const char *env = getenv("OM_BINDER_POOL_THREADS");
    if (env && atoi(env) > 0) n = (uint32_t)atoi(env);
    if (set_max) set_max(n);
    if (start) start();
}
