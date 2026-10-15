# OpenMontage GPU 原生化改造 — 工程报告

日期：2026-10-14　设备：Android 16 / ARM64 / MT6899（Mali-G720 MC8）/ Termux + PRoot Debian 12
分支：`feat/gpu-native-openmontage`（仅本地提交，未推送）　证据目录：`gpu_native/bench/results/`

> 结论先行：GPU/硬件优先已成为默认执行架构，并在真实 Mali GPU 与 MediaTek 硬件编码器上验证。主要生产路径（Remotion 渲染、全部 H.264/HEVC 编码、超分辨率、合成调度）均已迁移；剩余 CPU 工作负载均有实测或正确性依据。本次新增**免 proot 执行通道**（由 Termux 应用经服务 intent 派生 worker，**不需要 `allow-external-apps`、不改动任何 Android 设置**），在独占硬件编码上取得 **1.22×** 且输出**逐字节一致**；并行与流式作业实测无收益，故按独占门控启用。状态判定为 **PARTIAL**（理由见第 9 节）。
>
> **L5 修复（2026-10-14）**：针对独立 GPT-6.1 L5 验收提出的三个阻断项——**M1** 未显式 `-pix_fmt` 时的静默位深/色度降级与错误容器隐式编码器假设、**M2** 硬件编码器孤儿与输出所有权、**M3** 免 PRoot 路径字体命名空间不等价导致字幕变方框——已全部修复并通过前后实测验证。根因、前/后复现、回归测试与剩余不确定性见 **`gpu_native/REMEDIATION.zh-CN.md`**（修复提交 `a6bbb27`）。本报告不主张独立 L5 通过。
>
> **全域 L5 修复（2026-10-15）**：独立全域验收确认的两个 MAJOR——**F1** 生产输出所有权不完整（本地 `file:` URL、隐式 HLS/分片、Remotion 最终封装、FFmpeg compose 交付）与 **F2** FFmpeg `-c`/`-c:v` 选项顺序与作用域被改变（输出静默变成未转换的复制品且遥测冒报硬件编码）——已修复并给出前/后实测证据：根因、变更、SHA-256 与 ffprobe 证据、GPU 优先与回退验证、回归与性能见 **`gpu_native/REMEDIATION-F1-F2.zh-CN.md`**。本报告不主张独立 L5 通过。

---

## 1. 架构

### 根本约束（实测，不是假设）
- Debian（glibc）进程只能拿到软件 Vulkan/GL（llvmpipe / lavapipe / SwiftShader）。
- 真实硬件只对 **bionic（Android 链接器）进程** 可达：Mali GPU（Vulkan 1.3 / OpenGL ES 3.2）和 MediaTek 硬件编解码器（Codec2 / MediaCodec）。
- 因此架构采用「Debian 侧编排 + bionic 侧硬件执行」：OpenMontage 的 Python/Node 仍在 Debian 中运行，所有 GPU/硬件工作由项目私有的 bionic 运行时进程执行。

### 最终执行架构
```
Debian / glibc（OpenMontage Python、Node、Remotion CLI）
 ├─ lib/gpu/runtime.py      硬件探测与平台证明、策略（auto/cpu/require）、健康熔断、编码器会话槽
 ├─ lib/gpu/ffmpeg_exec.py  PATH 上的 ffmpeg 分发器（gpu_native/shims/path/ffmpeg）
 │    └─ lib/gpu/ffmpeg_args.py  libx264/libx265 → h264/hevc_mediacodec 的语义保持翻译
 ├─ lib/gpu/remotion.py     Remotion：Mali 上的 Chromium + 硬件编码钩子 + GPU 进程监视
 ├─ lib/gpu/superres.py     超分：ncnn/Vulkan Real-ESRGAN（Mali 计算）
 └─ lib/gpu/oop.py          免 proot worker 的客户端（仅用于独占编码的文件输出作业）
          │ 以 bionic 进程执行（仍在 proot 内）
          │        └─ 由 Termux 应用派生（am startservice）─▶ worker/om_gpu_worker.py
          │                                                  （bionic，TracerPid: 0）
          ▼
$OM_GPU_HOME = /data/data/com.termux/files/home/.openmontage-gpu
 ├─ prefix/      Termux FFmpeg 8.1 依赖闭包（MediaCodec + Vulkan），$ORIGIN RUNPATH
 ├─ browser/     Termux Chromium 138 headless_shell 闭包（ANGLE → Android GLES 驱动）
 ├─ realesrgan/  ncnn Real-ESRGAN + 已批准模型的 ncnn 版本
 ├─ run/         worker.sock / worker.ready（免 proot worker）
 ├─ bin/         omgpu-probe（平台证明探针）、零拷贝原型
 └─ state/       capabilities.json / chromium_gpu.json / health.json / exec.jsonl（执行日志）
```

### 默认路由（`OM_GPU_MODE=auto`，无需手动选择）
| 工作负载 | 默认后端 | 硬件类别 |
|---|---|---|
| 所有工具、Remotion、HyperFrames 的 H.264/HEVC 编码 | `c2.mtk.avc.encoder` / `c2.mtk.hevc.encoder` | 专用视频编码硬件 |
| Remotion 帧光栅化/合成 | Mali-G720（Chromium + ANGLE `gles-egl` → Android GLES 驱动） | GPU 图形 |
| 超分辨率（RealESRGAN_x4plus / x4plus_anime_6B） | Mali-G720（ncnn Vulkan 计算着色器） | GPU 计算 |
| 合成中相互独立的片段 | 并发调度（硬件编码会话） | 调度 |
| 视频解码、FFmpeg 滤镜、调色、音频 | CPU（见第 7 节例外说明） | — |

激活方式：导入 `tools` 包时调用 `lib.gpu.activate()`，仅对当前进程树把分发器放到 PATH 首位；不修改任何系统配置。`OM_GPU_MODE=cpu` 关闭，`OM_GPU_MODE=require` 禁止回退（验收用）。

### 免 proot 执行通道（新增，有界优化）
proot 会追踪其下**每个**进程，因此每次原生 FFmpeg 启动都要为 bionic 运行时的动态链接（83 个库）支付路径翻译开销：实测 `ffmpeg -version` 在被追踪时 1.42 s、不被追踪时 0.335 s。被追踪的进程**无法自行脱离**——PRoot 5.1 用 seccomp 过滤器（`Seccomp: 2`）在无追踪器时对被翻译的系统调用返回 `ENOSYS`，而 seccomp 过滤器只能叠加、不能移除。唯一的解是不经 proot 的进程来源：由 Termux **应用**派生的进程。

因此新增 `lib/gpu/oop.py`：用 `am startservice com.termux.service.action.service_execute`（**服务 intent**，不是 `RUN_COMMAND`）让 TermuxService 派生 worker；worker 经 Unix socket 执行原生命令。**这不需要 `allow-external-apps`，不申请权限，不改动任何 Android 设置**（此前报告将其列为前置条件，本次实测证伪）。派生出的 worker `/proc/self/status` 为 `TracerPid: 0`，可见 `/dev/mali0`。

该通道**有界**启用：应用派生的进程落在 Android `background` cpuset（仅 0–3 核，本机约慢 1.5×/核），故仅在作业**独占硬件编码器**时（`runtime.active_encoder_jobs("hwenc") == 0`）走 remote；任何失败都回退到原被追踪路径。

## 2. 实现（代码变更与集成的硬件后端）

提交：`94cce5b`（主体）及后续提交（见第 10 节）。主要文件：

| 位置 | 作用 |
|---|---|
| `lib/gpu/runtime.py` | omgpu-probe 平台证明（MediaCodec 类型 `HARDWARE_ACCELERATED`、Vulkan/EGL 设备与 `/dev/mali0`）；拒绝 llvmpipe/lavapipe/SwiftShader/`c2.android.*`；策略、健康熔断（15 分钟内 3 次 GPU 专属失败即跳过）、跨进程编码会话槽、`summary()` 路由表 |
| `lib/gpu/ffmpeg_args.py` | ffmpeg 命令解析；只改视频编码部分，其余参数逐字保留；CRF→CQ 经 VMAF 校准；保持 GOP/关键帧、色彩范围、CFR 等输出契约；无损/4:4:4/10-bit/强制关键帧/非整秒 GOP 保持 CPU |
| `lib/gpu/ffmpeg_exec.py` + `gpu_native/shims/path/ffmpeg` | 分发器：硬件运行→输出校验→失败时原样重跑 CPU 命令；只删除失败尝试自己写入/改写的文件；管道输入输出不可重放时仅在后端健康时走硬件；执行日志记录真实后端 |
| `lib/gpu/remotion.py` + `gpu_native/shims/chromium-gpu` | Remotion 用 bionic Chromium（ANGLE `gles-egl`）；`--binaries-directory` 钩子让帧编码走硬件编码器，其余调用仍用 Remotion 自带 FFmpeg；首次使用前验证 WebGL 渲染器为 Mali 且像素回读正确；渲染期间按 `/proc` 监视本次渲染的 GPU 进程，若退化为 `--use-gl=disabled` 则判定 `gpu-degraded` 并在 CPU 路径重渲染；默认并发 6 |
| `lib/gpu/superres.py` + `tools/enhancement/upscale.py` | 超分默认走 ncnn/Vulkan；2× 采用与 RealESRGANer 相同的 x4 网络 + Lanczos 缩放；GFPGAN、RealESRNet、带 alpha、16-bit 输入保留 PyTorch |
| `tools/video/video_compose.py` | 两条 Remotion 路径 GPU 优先；`compose` 片段在 GPU 路径并发编码；结果附带 `gpu_execution`；compose 临时目录改为每次调用唯一（修复并发任务互删临时文件的既有隐患） |
| `tools/video/remotion_caption_burn.py` | 字幕烧录的 Remotion 渲染 GPU 优先 |
| `tools/tool_registry.py`、`AGENT_GUIDE.md` | 预检 `provider_menu_summary()["gpu_runtime"]` 显示已验证硬件与路由表 |
| `config.yaml` / `lib/config_model.py` | `gpu.mode: auto` |
| `gpu_native/scripts/*` | 可复现引导：按 apt 索引 SHA256 校验抓取 Termux 包、`$ORIGIN` RUNPATH、去除 stub 库、编译原生组件；已从零在独立目录复现并验证 |
| `gpu_native/src/omgpu_probe.c`、`om_binder_pool.c` | 平台证明探针；binder 线程池垫片 |
| `lib/gpu/oop.py` | 免 proot 客户端：从**活的 proot 命令行**推导 guest→host 路径映射（`--rootfs` 与全部 `--bind`，最长前缀匹配，含滤镜串内嵌路径），用服务 intent 派生并复用 worker，`warm()`/`ensure_worker()`/`run()`/`stop()`；任何失败返回 `None` 让调用方回退 |
| `gpu_native/worker/om_gpu_worker.py` | bionic 侧常驻 worker（python3.13）：AF_UNIX + 长度前缀 JSON，线程化处理请求；客户端 socket 断开即杀掉子进程，避免孤儿占用编码会话槽 |
| `lib/gpu/ffmpeg_exec.py`（改） | 新增 `_run_native_remote()`、`OOP_LAST` 遥测字段；文件输出作业按独占门控走 remote，流式路径强制本地；作业开始时非阻塞 `oop.warm()` |
| `lib/gpu/runtime.py`（改） | 新增 `active_encoder_jobs()`：统计当前占用硬件编码会话槽数，用于独占判定 |
| `lib/gpu/remotion.py`（改） | GPU 渲染开始前 `oop.warm()`，给 worker 预热留出渲染时长 |
| `tests/lib/test_gpu_*.py` | 新增 46 个测试用例（翻译契约、GOP/范围/CFR 语义、回退数据安全、Remotion 运行器决策）+ `test_gpu_oop.py` 的 14 个（命名空间映射、worker 生命周期），另在 `test_basicsr_torchvision_compat.py` 增加 1 个 |

为让硬件可用而解决的关键问题（均已在 README 记录）：
- Termux `libandroid-stub` 的 stub `libmediandk.so` 使 MediaCodec 创建全部失败 → 从运行时移除。
- `LD_LIBRARY_PATH` 会泄漏到系统库解析 → 改用 `$ORIGIN` RUNPATH；运行时必须位于 `/data` 下才能命中 linker 的 `[system]` 命名空间。
- Codec2 依赖 binder 线程池；解码器先以默认 15 线程启动池会导致 FFmpeg 后续缩池时 abort → 预先以 FFmpeg 期望的大小启动。
- ANGLE Vulkan 后端需要 Android 加载器不提供的 `VK_KHR_xcb_surface`（包装 ICD 会使 GPU 进程 SIGSEGV）→ 改用 ANGLE `gles-egl` 走厂商 GLES 驱动。

## 3. 硬件证据（`results/hardware_evidence.json`）

| 类别 | 证据 |
|---|---|
| 专用视频编码硬件 | Android 16 `AMediaCodecInfo_getMediaCodecInfoType()`：`c2.mtk.avc.encoder`、`c2.mtk.hevc.encoder` = **HARDWARE_ACCELERATED**、vendor=true、最多 10 实例；FFmpeg 运行时日志：`MediaCodec encoder c2.mtk.avc.encoder`。对照：AV1 编码器为 `c2.android.av1.encoder` **SOFTWARE_ONLY**，系统从不把它当作硬件；VP9 无编码器。 |
| GPU 图形（Remotion） | `require` 模式真实渲染期间，Chromium GPU 进程命令行 `--type=gpu-process --use-angle=gles-egl --use-gl=angle`，**持有 `/dev/mali0`**，映射 `/vendor/lib64/egl/mt6899/libGLES_mali.so`、`arm.mali.platform-V2-ndk.so`；WebGL 渲染器 `ANGLE (ARM, Mali-G720 MC8, OpenGL ES 3.2)`，像素回读 `[64,128,191,255]` 正确。 |
| GPU 计算（超分） | ncnn 进程：`[0 Mali-G720 MC8] queueC=0[2]…`，**持有 `/dev/mali0`**，映射 `/vendor/lib64/hw/mt6899/vulkan.mali.so`。 |
| 设备本体 | Vulkan：`Mali-G720 MC8`，vendor 0x13b5，`INTEGRATED_GPU`，API 1.3.278，driverID 9（ARM proprietary），`v1.r49p1-03bet0`；EGL：`OpenGL ES 3.2 v1.r49p1…`，`/dev/mali0` 已打开。 |
| 对照（Debian/glibc） | Debian 侧 ICD 仅 lavapipe/freedreno/broadcom/radeon；前次审计证实只得到 llvmpipe。 |
| 免 proot 执行通道（新增） | 应用派生 worker 的 `/proc/<pid>/status`：`TracerPid: 0`（对比：proot 内任何进程 `TracerPid=<tracer>`）；父进程为 Termux 应用（uid 10382）；worker 内可见 `/dev/mali0` 且 guest `/root` 不存在，证明其运行在宿主命名空间。 |

另有反向证据：CPU 时间核算显示 GPU 路径 Chromium CPU 时间下降约 5 倍，与 CPU 光栅化不一致；系统从不以设备名推断硬件。

## 4. 性能（`results/final_suite.json`、`remotion_e2e_pair.json`）

测量说明：设备同时运行其他代理会话（codex/opencode/claude），CPU/GPU 运行交错进行；“媒体 CPU”为参与工作的全部进程（含 proot 追踪器）的 CPU 时间。

| 工作负载（真实工具） | CPU 路径（原有行为） | GPU 路径 | 结果 |
|---|---|---|---|
| Remotion 渲染 22 s 1080p30 演示（690 帧，含打包），背靠背同负载 | **398.6 s**，媒体 CPU ≈1415 核·秒 | **86.4 s**，≈228 核·秒 | **4.6×**，CPU 降 ~6× |
| 同上，较重背景负载 | 两次均超出工具自身 600 s 限制而**失败**（1625 核·秒） | 71.6 s / 98–100 s 成功 | GPU 把失败变为成功 |
| Remotion 渲染阶段 90 帧 1080p | 98.9 s，Chromium 189 核·秒 | 25.9 s，~39 核·秒 | 3.8× |
| compose（6 段、2 种源、加黑边、字幕，30.9 s 成片） | 67.0 s / 79.3 s，325–422 核·秒 | 26.9 s / 34.2 s，79–112 核·秒 | 2.3–2.5×，CPU 降 ~4× |
| 单次转码 1080p30 900 帧（`libx264 -crf 23` 命令） | 23.6 s / 42.1 s，134–213 核·秒，VMAF 93.65，8.9 MB | 15.4 s / 16.7 s，30–39 核·秒，VMAF 95.88，14.0 MB | 1.5–2.5×，质量更高，体积 ~1.6× |
| 超分 480×270→1920×1080（RealESRGAN_x4plus） | 154.9 s（PyTorch，524 核·秒） | 21.1 s（41 核·秒） | **7.3×** |
| 持续批量：6 个转码、3 并发、5400 帧 | 205.9 s，**26.3 fps** 聚合，1300 核·秒 | 58.5 s，**93.4 fps** 聚合，272 核·秒 | **3.55×**，0 失败 |

**免 proot worker 的 A/B（`results/proot_free_ab.json`，交错运行、同负载、只改 `OM_GPU_OOP`，3 次取中位数）**：

| 作业形态 | 被追踪（原路径） | 免 proot worker | 结果 |
|---|---|---|---|
| 独占 30 s 1080p 硬件编码 | 15.46 s | **12.67 s** | **1.22×**，省 2.79 s/次，输出**逐字节相同** |
| 6 个编码、3 并发 | 53.54 s | 51.45 s | 1.04×（噪声内）——仅独占的那一个作业符合条件 |
| Remotion 帧复用编码（`image2pipe`） | 146.5 s | 151.1 s | 0.97×（噪声）——worker 实际未被使用 |
| Remotion 式 stdin 管道编码专用实验（300 帧 MJPEG→h264_mediacodec） | 3.40 s | 3.60 s | 0.95×——**无收益** |

启动开销实测（`bench/oop_bench.py`）：原生 `ffmpeg -version` 被追踪 1.42 s vs 免 proot 0.335 s（每次启动省 ~1.09 s）；连续 5 次 7.09 s vs 1.75 s。独占编码的收益即来自“免去每次启动的动态链接路径翻译 + 编码期间的逐系统调用翻译”。

开销与瓶颈：
- proot 追踪器：约为 Chromium 自身 CPU 的 30%；每个原生 FFmpeg 启动 ~1.4 s（0.54 s 进程创建 + 加载 83 个库）。
- worker 的 cpuset 惩罚：应用派生进程仅用 0–3 核（本机约慢 1.5×/核），这是“仅独占时启用”的直接原因，也是并行批次与流式管道无收益的原因。
- 硬件编码器无 B 帧、码控较粗：同等或更高质量需 1.1–2.2× 码率（质量恒定，不是体积恒定）。
- Remotion 每次渲染的 webpack 打包（20–50 s）仍在 CPU，两条路径相同。
- Remotion 并发：GPU 路径实测 6 最好（4/5/8 在噪声范围内略差）。

## 5. 兼容性与正确性

- **免 proot 输出逐字节一致**：worker 与被追踪路径对同一命令产出 **sha256 完全相同**的文件——独占单次编码 `bf7b46e8…`、stdin 管道实验 `c29af9af…`（四份全部相同）；帧数 900/300、分辨率 1920×1080 一致。两条路径调用的是同一个硬件编码器与同一份参数，故不引入任何输出差异。
- **编码质量**（`results/encoder_calibration.md`）：两个真实素材上 CQ 映射保证 VMAF ≥ libx264 同 CRF（CRF18→CQ98：96.25→96.51 / 99.00→99.23；CRF23→CQ90：93.65→95.88 / 97.04→98.86）。
- **输出契约一致性矩阵**（`results/parity_matrix.json`，10 种工具命令形态，CPU vs GPU）：**全部 10 例帧数、起始时间、时长一致**；GPU 对 CPU 的 PSNR 45–50 dB（最小 ≥43.4 dB）。差异仅为色彩范围标签：未标注 vs `tv`（数据同为有限范围）；调色例中 CPU 路径实际输出 **yuv444p（High 4:4:4）** —— 这是原有 CPU 路径的兼容性缺陷（多数浏览器/手机无法解码），GPU 路径输出 4:2:0 并正确标注。
- 验证中发现并修复的语义差异（若不修复会破坏冻结要求）：
  1. 字幕字体回退不同（Arial→DejaVu Serif vs Liberation Sans）→ 设置 `FONTCONFIG_PATH=/etc/fonts`，11 个字体族与 Debian libass 选择完全一致；
  2. FFmpeg 5.1 中 `-pix_fmt yuv420p` 隐含转为有限范围，8.1 保留源范围 → 按被替代的 CPU FFmpeg 版本复现（Remotion 自带 7.1 不加，Debian 5.1 加 `-color_range tv`）；
  3. FFmpeg 5.1 对 MP4 默认 CFR（VFR 源会补帧），8.1 默认 VFR → 对 5.1 参考自动加 `-fps_mode cfr`（VFR 源 119 帧，与 CPU 一致）；
  4. 硬件 I 帧间隔为整秒且忽略 `-force_key_frames` → 仅整秒 GOP 走硬件，其余保留 libx264（修复了 6 个 video_trimmer 回归）。
- **Remotion**：GPU vs CPU 渲染 SSIM 0.9979、PSNR 45.6 dB，文字字形在 2× 放大下一致；三个内置演示（world-in-numbers、focusflow-pitch、code-to-screen）均正确成片。
- **音画同步**：compose GPU/CPU 成片按时间戳对齐后 PSNR 46.6 dB（最小 43.7）/ VMAF 95.2；字幕出现/消失帧时刻一致。
- **超分**：GPU（fp16）与 PyTorch（fp32）输出 PSNR 49.2 dB、SSIM 0.998，视觉一致。
- **既有测试**：`tests/tools + tests/contracts + tests/lib` 共 1978 项（项目 `.venv`）：1977 通过、7 跳过、3 预期失败、1 失败 —— 该失败为预检输出形状测试（新增 `gpu_runtime` 字段），已按更新后的 AGENT_GUIDE 契约修正测试，该文件复跑 51/51 通过。另有两处既有测试按新契约调整：上采样工具在 GPU 后端可用时即为可用（不再仅由 PyTorch 决定）；Remotion 渲染命令中 GPU 参数插入在调用方首个 `--` 选项之前以保持原命令布局。

## 6. 可靠性（`results/soak_summary.json` 等）

- **无人值守混合队列**（2 个工作者，真实工具）：3 个 Remotion 渲染、4 个 compose、图像与视频超分，共 9 个任务 → **9/9 成功、9/9 输出校验通过**，302 s；其间对一个正在进行的硬件编码进程发送 **SIGKILL**，分发器检测到 `exit -9` 并对该片段原样重跑 CPU 命令，任务仍产出有效成片。
- GPU 渲染中途退化检测：强制 GPU 进程失败时，watcher 识别 `--use-gl=disabled` 并在 CPU 路径重渲染，不会把 CPU 结果标为 GPU。
- **免 proot worker 的隔离与无孤儿**：本次 A/B 共 18 条记录、**0 次超时**；累计 40+ 次硬件编码（混合被追踪与 worker 路径）无卡死。worker 在线程里监视客户端 socket，连接断开即杀掉自己已启动的子进程——恰好消除了此前观察到的“编码进程在父进程被杀后卡在 binder/futex、占住会话槽”的孤儿失败模式（另见第 8 节说明）。
- 超出硬件能力（8K 编码）→ 自动回退 libx264，输出正确。
- 数据安全：回退只删除失败尝试本身创建/改写的文件；已存在且未被改动的用户文件不动（单测覆盖）；未修改任何源素材。
- 熔断与会话槽：连续 GPU 专属失败会暂时跳过该后端；硬件编码会话有跨进程上限，批量任务排队而不是在 configure 时失败。
- 可复现：两个引导脚本在全新目录从零构建并通过全部硬件验证。

## 7. 仍在 CPU 上的工作负载及依据

| 工作负载 | 保留原因（分类） | 依据 |
|---|---|---|
| 源视频解码 | **实测更高效** | 多线程软件解码 300 fps；MediaCodec 缓冲模式 75–100 fps 且只省 ~28% CPU；硬件解码→ImageReader ~80 fps |
| FFmpeg 滤镜（scale/pad/crop/fps/overlay/drawtext/libass） | **实测更高效** | FFmpeg Vulkan 上传+下载本身 ~8 ms/帧（上限 89 fps）；模糊、缩放在 Mali 上更慢 |
| 调色（colorbalance/curves/eq） | **具体未解决的阻碍**（正确性） | libplacebo LUT 路径快 1.4×、CPU 降 4×，但前 2 帧为垃圾帧且与 swscale 存在 ~34 dB 色彩模型差异；需与 swscale 精确匹配的自研着色器 |
| 零拷贝 解码→GLES→编码 引擎 | **实测更差**（已实现原型） | Mali 上端到端可用，但编解码 Surface 路径串行：单任务 29–34 fps，3 任务聚合 60 fps，对比混合路径 122 fps |
| 音频（混音、AAC、atempo、响度） | **技术适配性** | 计算量小，无 GPU 收益 |
| Remotion 中的 JS/React/布局、截图 JPEG 编码、CDP 传输、OffthreadVideo 抽帧（Rust 合成器） | **技术适配性 / 缺少硬件实现** | 浏览器主线程工作本质为 CPU；合成器是 glibc 进程无法使用硬件解码器 |
| 无损、4:4:4、10-bit、强制关键帧、非整秒 GOP、VP9/AV1 编码 | **技术适配性 / 缺少硬件实现** | 硬件编码器不支持或会改变输出契约；AV1 编码器本身是软件实现 |
| 未显式指定输出 `-pix_fmt` 的工具（`color_grade`、`eye_enhance`、`face_enhance`、`auto_reframe`、`silence_cutter`、`video_trimmer` 等；`video_compose`/`video_stitch` 仅其无 `-pix_fmt` 的分支） | **正确性（L5-M1）** | 无显式 `-pix_fmt` 时无法确证位深/色度等价，硬件只输出 8-bit 4:2:0；改为保留原 CPU 命令，避免静默降级 |
| 字幕 / ASS / drawtext 等文字栅格化命令 | **兼容性（L5-M3）** | 免 PRoot worker 不可见 Debian 字体栈；路由到受追踪原生路径（仍走硬件编码，字体选择与 CPU 一致） |
| GFPGAN、RealESRNet、rembg、Whisper、Piper、OpenCV 人脸/眼部/抠像 | **缺少硬件实现** | 本环境无 PyTorch/ONNX Runtime 的 Mali 后端；转换为 ncnn 会改变已批准模型的实现，需单独批准与验证 |

## 8. 局限（未夸大）

1. **proot 开销只在“独占的文件输出编码”上被消除**：新增的免 proot 通道需由 Termux 应用派生进程，而应用派生进程落在 Android `background` cpuset（仅 0–3 核，本机约慢 1.5×/核），因此**仅在作业独占硬件编码器时启用**（实测 1.22×）。并行批次（1.04×）与 Remotion 帧复用 / `stdin` 管道编码（0.95–0.97×）无收益：前者多个编码共享 0–3 核抵消了追踪开销，后者分发器自身必须（在被追踪状态下）转发管道，收益被 cpuset 惩罚抵消。把 worker 提到大核需要 `am start` 拉起 Termux 活动——被后台启动限制拒绝，属 Android 状态变更，本次未做。**注意：本通道用服务 intent（`startservice`），实测不需要 `allow-external-apps`，也未申请任何权限或改动 Android 设置**（此前报告把该开关列为前置条件，本次已证伪）。
2. **HyperFrames 未安装**：其编码会自动经 PATH 分发器走硬件，但其浏览器 GPU 化未能验证。
3. **Chromium 版本差**：GPU 浏览器为 Termux Chromium 138（Debian 为 153）。已在内置组合上验证；依赖 138 之后新 Web 特性的 atelier 组合可能渲染不同（失败会回退，但静默的视觉差异无法自动发现）。
4. **码率代价**：同等质量硬件编码码率 1.1–2.2×；有体积上限的导出应显式给 `-b:v`。
5. **调色 GPU 化未完成**（见第 7 节）。
6. **测量条件**：共享设备、其他代理会话同时运行，单次运行波动很大（同一 Remotion 渲染 77–161 s，同一被追踪编码 11–42 s）；因此本次 A/B 采用**交错 + 中位数**，只对稳定的差值下结论。Remotion 作业的墙钟差异经查**不是**本改动所致——其帧编码是 `image2pipe` stdin 管道，worker 未被使用（遥测 `oop` 缺失）。连续负载最长约 10 分钟级，未做数小时热稳定性测试。
7. **一次未复现的卡死**：早前观察到一次**被追踪的**硬件编码在父进程被杀后卡在 `binder_thread_read`/`futex`（49 线程）并占住会话槽；随后 40+ 次编码（含被追踪与 worker 路径）均正常，未能归因于本次改动；worker 路径另有“客户端断开即杀子进程”的保护。
8. **运行时位置**：`/data/data/com.termux/files/home/.openmontage-gpu` 不在 NO_DELETE 受保护列表中（我未修改该策略文件）；建议用户将其加入。
9. 引导依赖本机 Termux apt 索引快照中的包版本；镜像下架旧版本时需刷新索引。
10. **输出所有权收紧带来的拒绝语义**（`3268e8e`）：被加速的命令若同时含有无法接管的文件输出（编号 pattern 或多文件 muxer 如 `hls/segment/tee`），分发现以 `rc=78` 明确失败，而不是继续执行未保护的直写。这是刻意的安全收紧；仓库现有工具不含该组合，且纯多文件 muxer 命令不再被翻译、照旧在 CPU 直通。详见 `REMEDIATION.zh-CN.md` 第 11.8 节。

## 9. 状态：**PARTIAL**

已完成并验证：GPU 优先为默认（A）、三类真实硬件执行证据（B）、CPU/GPU 对比与端到端结果（C）、功能/质量/同步兼容（D）、无人值守可靠性与故障注入（E）、CPU 例外逐项说明（F）；另新增**免 proot 执行通道**（服务 intent，不改 Android 设置），在独占硬件编码上取得 **1.22×**、输出**逐字节一致**（G）。
未闭合：调色的 GPU 等价实现（正确性阻碍）、HyperFrames 的 GPU 浏览器验证（未安装）、proot 开销在并行批次与流式管道作业上的消除（受“应用派生进程只能用 0–3 核”的限制，需 Android 状态变更）。因此不报告为 COMPLETE。

**L5 修复（`a6bbb27`）后的状态变化**：三个阻断项已闭合，GPU 优先默认与硬件路径、CPU 回退均保持；其中 M1 使“未显式 `-pix_fmt`”的工作负载按正确性保留在 CPU（见第 7 节新增行），M3 使文字栅格化命令走受追踪原生路径（仍硬件编码）。状态仍为 **PARTIAL**，但不再包含上述三项阻断。详见 `gpu_native/REMEDIATION.zh-CN.md`。

**L5 复审补修（`205a370`）后的状态变化**：独立复审确认 M1/M3 已闭合，但 **M2 仍阻断**——CPU 回退直接写生产路径（带 `-y`）、取消后其子进程存活并继续写、多输出失败会破坏既有资产。第二轮把「逐次尝试暂存 + 受控子进程（独立会话 + `PR_SET_PDEATHSIG` + 进程组信号转发）+ 事务化整组发布（预检 + 硬链接备份 + 回滚）+ 取消即弃」统一施加到硬件尝试与 CPU 回退之上，并以真实子进程复现前/后证据（失败/取消用例的既有产物 SHA-256 逐字节不变，回退写入者不再存活）。状态仍为 **PARTIAL**。详见 `gpu_native/REMEDIATION.zh-CN.md` 第 10 节。

**L5 复审补修（`3268e8e`）后的状态变化**：最终复验指出第 2 轮事务仍只覆盖 `gpu_plans`——同一命令里的 **stream copy / 音频 / 图片** 等文件输出未被暂存，实测把既有 20,897,137 字节成片与其音频截断为 0 字节。第三轮改为对**整条命令的全部输出**先分类（单一路径文件 / 管道 / 非文件目标 / 无法接管），对全部单一路径文件输出统一做「逐次暂存 + 按媒体类型校验 + 事务化发布 + 失败回滚」（硬件尝试、CPU 回退、流式命令一致），**无法接管时在改写任何文件前以 `rc=78` 拒绝**。前/后实测：混合输出失败与取消用例的既有资产 SHA-256 逐字节不变、无残留临时文件与存活写入者，多输出成功仍同事务发布，真实硬件编码输出 SHA-256 不变且 `backend=gpu`。状态仍为 **PARTIAL**。详见 `gpu_native/REMEDIATION.zh-CN.md` 第 11 节。

## 10. 仓库状态、测试与复现

- 分支 `feat/gpu-native-openmontage`；提交与 diff：`git log c135813..HEAD`、`git diff c135813..HEAD --stat`。L5 修复提交：`a6bbb27`（父 `585eed3`）；L5 复审补修（M2 生命周期）：`205a370`；L5 复审补修（M2 全命令输出所有权）：`3268e8e`。
- 测试：新增 `tests/lib/test_gpu_ffmpeg_args.py`、`test_gpu_dispatch_safety.py`、`test_gpu_remotion_runner.py`、`test_gpu_oop.py`（命名空间映射与 worker 生命周期，14 项）；GPU 层 4 个文件在 M2 全命令输出所有权补修后共 **127 项全部通过**（`test_gpu_oop` 14 + `test_gpu_ffmpeg_args` 54 + `test_gpu_dispatch_safety` 41 + `test_gpu_remotion_runner` 6），其中 `test_gpu_dispatch_safety.py` 由 12 项扩到 **28 项**（第 2 轮）再到 **41 项**（第 3 轮：混合输出暂存/发布、流式命令的文件输出暂存、编号 pattern 与多文件 muxer 拒绝、无法定位即拒绝、覆盖拒绝直通、媒体类型校验）；全量 `pytest tests/ -q` 为 **2117 passed, 11 skipped, 3 xfailed**。
- 复现：`bash gpu_native/scripts/bootstrap_runtime.sh && bash gpu_native/scripts/bootstrap_browser.sh`；证据：`python gpu_native/tools/collect_evidence.py <dir>`；基准：`gpu_native/bench/final_suite.py`、`parity_matrix.py`、`soak.py`；免 proot A/B：`gpu_native/bench/oop_ab.py`（交错、中位数）与 `oop_bench.py`（每次启动开销），结果见 `results/proot_free_ab.json`。
- 新增环境开关：`OM_GPU_OOP`（默认 `auto`；`0`/`off` 关闭）、`OM_GPU_OOP_WAIT_S`（默认 2.5）、`OM_GPU_OOP_TIMEOUT_S`（默认 3600）。所有开关只影响本工程的执行路径选择，不改变任何输出契约。
- Develop Record：`DEVELOP_RECORD = REQUIRED`，但 openmontage 无受治理绑定（候选为无关项目）；按协议未创建项目/模块/Slice，**本次未写入开发记录**。
