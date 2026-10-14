# 独立 L5 验收三项阻断项的修复报告（M1 / M2 / M3）

日期：2026-10-14
设备：Android / ARM64 / MediaTek MT6899（Mali-G720 MC8）/ Termux + PRoot Debian 12
分支：`feat/gpu-native-openmontage`（仅本地提交，未推送、未合并）
修复提交：`a6bbb27`　被阻断版本：`585eed3`

> 结论先行：独立 GPT-6.1 L5 验收给出的三个阻断项——**M1 未显式指定像素格式时的静默位深/色度降级**、**M2 硬件编码器孤儿与输出所有权（部分产物当成功发布的风险）**、**M3 免 PRoot 路径字体命名空间不等价导致字幕变方框且仍 `rc=0`**——已全部定位根因、修复并给出**前/后实测证据**。GPU 优先默认（`OM_GPU_MODE=auto`）、Mali/MediaTek 硬件路径与 CPU 回退均保持不变；未改动产品契约，未弱化既有测试。
>
> **本报告不主张独立 L5 通过。** 复现与判定应由独立方按其自身用例重跑。

---

## 0. 范围与边界

- 只修 M1/M2/M3 三项；不重构、不做投机优化、不改动 Android 权限/安全/内核/GPU 驱动。
- 保持 GPU 优先：没有为了“过测试”而禁用 GPU；显式 `-pix_fmt yuv420p` 的工作负载仍走硬件（见 §1.3）。
- 未修改产品契约；仅更正了“把缺陷行为固化下来”的既有单测（见 §1.4）。
- 全部修复为**本地提交**，未推送、未合并。

---

## 1. 发现一：未显式指定像素格式时的静默降级

### 1.1 根因

`lib/gpu/ffmpeg_args.py`：

1. `_SUPPORTED_PIX` 含 `None`（`{None, "yuv420p", "yuvj420p", "nv12"}`）。命令没有 `-pix_fmt` 时翻译仍把 `-c:v libx264` 换成 `h264_mediacodec`。硬件编码器只产出 8-bit 4:2:0，于是 **10-bit 源被静默降为 8-bit、4:4:4 被静默降为 4:2:0，且 `rc=0`**。CPU 侧本会依据输入与滤镜图推断格式（10-bit 保持 10-bit），因此这是语义改变。
2. `_DEFAULT_H264_FORMATS` 错误地把 `mpegts / flv / 3gp`（以及重复集合里的 `ts/mts/3gp/m4v`）当作“隐式默认 H.264”。实测这些容器的隐式视频编码器并非 H.264，翻译会**改变 codec family**。

用 `ffmpeg -h muxer=<m>` 实测（本机 Debian FFmpeg）：

| 容器 | 隐式默认视频编码器 |
|---|---|
| mp4 / mov / ipod / matroska | h264 |
| mpegts | mpeg2video |
| flv | flv1 |
| 3gp | h263 |
| m4v | mpeg4 |

### 1.2 修复

- `_SUPPORTED_PIX` 去掉 `None`：只有显式 `-pix_fmt yuv420p|yuvj420p|nv12` 才允许翻译到硬件；未指定时**保留原 CPU 命令**并给出明确原因。
- `_DEFAULT_H264_FORMATS` 收敛为 `{mp4, mov, matroska, mkv, ipod}`，删除重复集合。

语义原则：**仅当位深/色度/codec family 可确证等价时才翻译；无法可靠推断时保留原 CPU 命令。正确性优先于性能。**

### 1.3 前/后实测（用验收方自己的 `live_media.py`，ColorGrade + `custom_vf='null'`）

| 输入像素格式 | CPU 输出 | auto 输出（修复前 `585eed3`） | auto 输出（修复后 `a6bbb27`） |
|---|---|---|---|
| yuv420p | High / yuv420p | High / yuv420p | High / yuv420p（硬件） |
| yuv420p10le | High 10 / yuv420p10le | **High / yuv420p**（backend=gpu, h264_mediacodec, rc=0） | High 10 / yuv420p10le（CPU，明确原因） |
| yuv444p | High 4:4:4 Predictive / yuv444p | **High / yuv420p**（backend=gpu, rc=0） | High 4:4:4 Predictive / yuv444p（CPU，明确原因） |

修复前遥测证明这是**成功路径上的静默降级**，而非失败回退：

```
{"backend": "gpu", "encoder": "h264_mediacodec", "device": "c2.mtk.avc.encoder", "rc": 0,
 "output": ".../matrix-yuv420p10le-auto.mp4"}
```

修复后遥测为 CPU 且带原因：

```
{"backend": "cpu",
 "fallback_reason": "output pixel format not specified (hardware encoder would force 8-bit 4:2:0)",
 "output": ".../matrix-yuv420p10le-auto.mp4"}
```

**GPU 优先仍然保留**（验收方 AV-2997 用例，显式 `-pix_fmt yuv420p`）：

```
AV-2997 mode=auto rc=0 -> High / yuv420p range=tv
遥测: backend=gpu encoder=h264_mediacodec oop=false
```

### 1.4 回归测试

`tests/lib/test_gpu_ffmpeg_args.py`：

- `test_unspecified_pixel_format_stays_on_cpu`（新增）
- `test_implicit_non_h264_container_encoders_stay_on_cpu[mpegts|flv|3gp|m4v]`（新增）
- `test_implicit_h264_containers_are_translated`（新增）
- 原有“翻译”单测补上显式 `-pix_fmt yuv420p`：这些用例真正要验证的是 GOP/同步/profile/codec-family，原先依赖“缺省 pix_fmt 也翻译”这一缺陷行为，属于**更正了固化缺陷的测试**，不是弱化契约。

---

## 2. 发现二：硬件编码器孤儿与输出所有权

### 2.1 根因（三处，缺一不可）

1. `gpu_native/worker/om_gpu_worker.py`：编码子进程**没有登记**；`SIGTERM/SIGINT` 处理直接 `os._exit(0)`，不会终止正在运行的编码；编码从 handler 线程派生，没有 `PR_SET_PDEATHSIG` 兜底 → worker 消失后编码器**继续存活并写文件**。
2. `lib/gpu/ffmpeg_exec.py`：硬件输出**直接写最终路径**（带 `-y`）；`_remove_partial` 只能保护“未被触碰的旧文件”，无法恢复被 `-y` 覆盖的旧产物；重试可能与仍在运行的孤儿编码**并发写同一输出**。
3. `lib/gpu/oop.py`：客户端断连（EOF）后**盲目重发同一请求**，可能产生第二个编码器并发写同一目标。

### 2.2 修复

- worker：新增活动子进程登记表 + 锁；子进程 `start_new_session=True`（独立进程组）并在 `preexec_fn` 设 `PR_SET_PDEATHSIG=SIGKILL`；`stop()`（SIGTERM/SIGINT）在退出前 `killpg` 掉所有子进程。
- dispatcher：每个 GPU 文件输出先写到**同目录唯一临时名** `.om-gpu-<pid>-<rand><ext>`，仅在**校验通过后**用 `os.replace` 原子提交；失败则删除临时文件。保留“无 `-y` 且目标已存在则拒绝覆盖”的语义。孤儿或重试因此无法污染已发布输出或用户既有文件。
- `oop.run`：删除断连后的盲目重放；失败由上层回退到受 PRoot 追踪的原生路径（已验证兼容）。

### 2.3 前/后实测（`/tmp/om/m2_verify.py`：真实 bionic FFmpeg 写入中途杀 worker）

| 信号 | 修复前 `child_alive_final` | 修复前 输出增长 | 修复后 `child_alive_final` | 修复后 输出增长 |
|---|---|---|---|---|
| SIGTERM | true（存活） | 524336 → 6029360 字节 | false | 冻结在 524336 |
| SIGKILL | true（存活） | 524336 → 6029360 字节 | false | 冻结在 524336 |

### 2.4 暂存 / 原子提交实测

- **成功路径**（真实硬件编码）：运行期间目录里**只有** `.om-gpu-19250-e58bb832.mp4`，最终名不存在；结束后最终文件已发布、无临时残留；`backend=gpu, oop=true, device=c2.mtk.avc.encoder`。
- **失败路径**（`/tmp/om/m2_staging_evidence.py`：让硬件尝试写完垃圾后失败）：GPU 尝试期间既有用户文件内容**未被触碰**（仍为原字节）；临时文件被丢弃、无残留；CPU 回退产出**完整校验通过**的视频，`rc=0`；遥测 `backend=cpu` 带原因。伪造“硬件成功”时则原子提交、`backend=gpu`。
- **生产实证**：真实运行的 bionic FFmpeg argv 输出目标就是暂存名——`.../tmp/om/.om-gpu-30441-833ea612.mp4`。

### 2.5 回归测试

`tests/lib/test_gpu_dispatch_safety.py`（共 12 项）：

- `test_stage_outputs_rewrites_to_a_sibling_temp_with_same_extension`
- `test_stage_outputs_refuses_existing_file_without_overwrite_flag`
- `test_stage_outputs_allows_existing_file_with_yes`
- `test_stage_outputs_skips_pipe_targets`
- `test_stage_outputs_returns_none_when_target_token_is_absent`
- `test_commit_publishes_atomically_and_discard_leaves_no_trace`
- `test_discarded_attempt_never_touches_the_final_output`
- `test_each_attempt_uses_a_distinct_temp`
- `test_fallback_never_deletes_untouched_preexisting_output`
- `test_fallback_removes_file_the_gpu_attempt_created`
- `test_fallback_removes_file_the_gpu_attempt_rewrote`
- `test_real_ffmpeg_skips_the_shim_directory`

（流式/管道输出本就无回退、无暂存；其子进程存活由 dispatcher 的 `PR_SET_PDEATHSIG` 绑定，不受影响。）

---

## 3. 发现三：字幕字体损坏

### 3.1 根因

免 PRoot worker 运行在 Android 宿主命名空间：Debian 的 `/usr/share/fonts` 与 `/etc/fonts` **在其内不可见**，只能看到映射过来的 `rootfs/root/.local/share/fonts`（仅 NotoSansCJK，不含阿拉伯字形）。于是 libass 逐字输出 “failed to find any fallback with glyph”，画面出现方框，但 `rc=0`、`backend=gpu`、`oop=true`。

### 3.2 为什么不能靠“把字体配置喂进 worker”解决（等价性不成立）

逐一实测（同一命令 CPU vs worker 对比）：

| 方案 | 缺字行数 | 实际字体选择 | 与 CPU 画面差异 |
|---|---|---|---|
| 沿用当前映射 | 1080（方框，复现缺陷） | NotoSansCJK 单字体 | 大（方框） |
| `FONTCONFIG_SYSROOT`（相对/映射） | 0 | `Arial → DejaVuSerif`（别名不解析） | 8.6–11.4 |
| 生成显式 fonts.conf | 0 | `Arial → DejaVuSerif` | 8.6–11.4 |
| **受 PRoot 追踪的原生路径** | **0** | `LiberationSans → DejaVuSans → NotoSansCJK`（与 CPU 一致） | **0.635（仅编码器差异）** |

前几种虽“不再缺字”，但字体选择与 CPU 不一致（如 `Arial → DejaVuSerif`），**排版语义不等价**，故不作为修复。按验收要求（“原生路径无法复现排版时，走既有已验证兼容路径”），**把文字栅格化命令路由到受 PRoot 追踪的原生路径**。

### 3.3 修复

- `lib/gpu/ffmpeg_args.py:renders_text(argv)` 检测文字栅格化：`subtitles|ass|drawtext|showtext` 滤镜、`fontfile|fontsdir|font|textfile` 选项、`.ttf/.otf/.ttc/.pfb/.pfa` 字体文件；并加前导边界，避免 `bass=f=80` 被误判为 `ass`。
- `lib/gpu/ffmpeg_exec.py`：`allow_remote = solo and not text_render` → 文字命令走受追踪原生路径（**仍用硬件编码**，但读 Debian 字体栈），遥测新增 `text_render`。

硬件加速未丢失：受追踪原生路径仍 `backend=gpu`、`h264_mediacodec`、`device=c2.mtk.avc.encoder`。

### 3.4 前/后实测（真实渲染帧，非 rc）

**(a) 阿拉伯语（验收方用例 `arabic_subtitles.py`）**

| 路径 | 与 CPU 帧 mean\|Δ\| | 字体选择 | 遥测 |
|---|---|---|---|
| CPU（参照） | 0.000 | LiberationSans→DejaVuSans | — |
| 修复前 worker | 方框 | NotoSansCJK 单字体 | backend=gpu, oop=true |
| 修复后直连命令 | **0.635** | LiberationSans→DejaVuSans | backend=gpu, encoder=h264_mediacodec, device=c2.mtk.avc.encoder, oop=**false**, text_render=**true** |

修复后 worker 与 traced 的 mean\|Δ\| **完全相同（均 0.635，maxΔ 均 65）**，即排版逐像素一致。

**(b) 阿拉伯语真实工具（验收方 `compose_arabic.py`：VideoCompose 合成 + 字幕）**

| 版本 | auto 与 CPU 帧 mean\|Δ\| | 文字墨水像素 | 硬件 |
|---|---|---|---|
| 修复前（方框） | 14.228（max 255） | 758795（方块） | oop=true，两个硬编步骤 |
| 修复后 | **0.347（max 52）** | 730409（CPU=730534） | 分段编码 backend=gpu/oop=true；字幕步骤 backend=cpu 带 M1 原因 |

两个修复正确组合：字幕步骤因未指定 `-pix_fmt`（M1）走 CPU、字体正确；分段编码仍走硬件。

**(c) 既有中文用例**（本仓库既有字幕烧录路径 `VideoCompose.burn_subtitles`，用例见 `tests/qa/test_05_video_compose.py`；仓库内无中文 fixture，本次用中文 SRT 走**同一条既有路径**）

| 模式 | success | 与 CPU 帧 mean\|Δ\| | 路由 |
|---|---|---|---|
| cpu | true | 0.000（参照） | — |
| auto | true | **0.0（max 0）** | backend=cpu（该路径无 `-pix_fmt` → M1 规则），中文渲染与 CPU 完全一致 |

**(d) 中/英/阿混合、显式 `-pix_fmt yuv420p`（`/tmp/om/m3_verify.py`）**：`missing_glyph_lines=0`，字体集合与 CPU 同族（LiberationSans/DejaVuSans/NotoSansCJK），`backend=gpu, oop=false, text_render=true`，mean\|Δ\|=0.666（≈ 编码器差异基线）。

### 3.5 回归测试

`tests/lib/test_gpu_ffmpeg_args.py`：`test_text_rendering_commands_are_detected[...]`、`test_non_text_commands_are_not_flagged[...]`（含 `bass=f=80` 反例）。

---

## 4. 硬件执行真实性（修复后实测）

运行中的 bionic FFmpeg 子进程（经免 PRoot worker 派生）：

- argv 含 `-c:v h264_mediacodec`，输出目标为暂存名 `.om-gpu-30441-833ea612.mp4`；
- 链接 Android 媒体栈：`libcodec2*.so`、`libmediandk*.so`；
- `TracerPid: 0`（确实不经 PRoot）；
- 设备 `mt6899`；编码组件 `c2.mtk.avc.encoder`（`max_instances=10`）；
- 遥测：`backend=gpu, encoder=h264_mediacodec, device=c2.mtk.avc.encoder, oop=true`。

---

## 5. 性能

`gpu_native/bench/oop_ab.py --only single --reps 3`（交错、取中位数；30 s / 900 帧 1080p，显式 `-pix_fmt yuv420p`）：

| 路径 | 中位数 |
|---|---|
| 受追踪（`OM_GPU_OOP=0`） | 11.47 s |
| 免 PRoot worker | 9.78 s |

约 **1.17×**，与历史 1.22× 同向；暂存/原子提交未引入可测回归。

---

## 6. 测试

- 全量：`2075 passed, 11 skipped, 3 xfailed`，`EXIT=0`（`pytest tests/ -q`）。
- 三个 GPU 定向模块（`test_gpu_ffmpeg_args` + `test_gpu_dispatch_safety` + `test_gpu_oop`）：**80 passed**。

---

## 7. 影响面与剩余不确定性

1. **未指定 `-pix_fmt` 的生产工具现在留在 CPU**（正确性取舍）。静态核实“直接构造命令且全文件无 `-pix_fmt`”的文件：`tools/enhancement/color_grade.py`、`tools/enhancement/eye_enhance.py`、`tools/enhancement/face_enhance.py`、`tools/video/auto_reframe.py`、`tools/video/silence_cutter.py`、`tools/video/video_trimmer.py`。`video_compose`/`video_stitch` 为混合：走 `lib/media_profiles`（会输出 `-pix_fmt`）或显式写 `-pix_fmt` 的路径**仍走硬件**（已实测合成主路径 `backend=gpu, oop=true`）。这些工具若希望恢复硬件路径，应显式声明输出 `-pix_fmt`（属产品决策，本次未改契约）。
2. 文字栅格化命令不再走免 PRoot worker（保留硬件编码，但少了免 PRoot 的少量提速）。受追踪原生路径对 CJK 可能选到 NotoSansCJK 的不同地区字形（JP vs KR），与 CPU 相比 mean\|Δ\| ≤ ~0.7，与硬件编码器差异同量级，**无缺字**；这是追踪路径既有特性，非本次引入。
3. worker 的 `preexec_fn` 用于设 `PR_SET_PDEATHSIG`，在多线程进程中有已知的（极低概率）fork/exec 死锁告警；与 dispatcher 既有用法一致，本设备实测工作正常。
4. **部署侧**：免 PRoot worker 为长驻进程；生命周期修复需**重启 worker** 才生效。本机已重启（新 worker PID 32535，`TracerPid: 0`）。若独立验收复用修复前启动的 worker，M2 行为仍为旧实现——请先重启。
5. 字体等价性只覆盖本机已验证的 Debian 字体栈；其他 fontconfig 配置未穷举。容器隐式编码器默认值只覆盖上表所列。
6. 流式/管道输出无暂存与回退（设计如此）；其子进程存活由 `PR_SET_PDEATHSIG` 绑定。
7. 本报告**不构成独立 L5 通过结论**。

---

## 8. 复现步骤

```bash
# M1（前/后）
.venv/bin/python /tmp/om/m1_matrix.py                 # 修复后：三种格式与 CPU 一致
git checkout 585eed3 -- lib/gpu gpu_native/worker/om_gpu_worker.py
.venv/bin/python /tmp/om/m1_matrix.py                 # 修复前：10bit/444 被降为 yuv420p/High
git checkout HEAD -- lib/gpu gpu_native/worker/om_gpu_worker.py

# M2 孤儿
.venv/bin/python /tmp/om/m2_verify.py                                      # 修复后：child_alive_final=false，输出冻结
OM_M2_WORKER=/tmp/om/om_gpu_worker_old.py .venv/bin/python /tmp/om/m2_verify.py   # 修复前：孤儿存活并继续写
# M2 暂存/所有权
.venv/bin/python /tmp/om/m2_staging_evidence.py

# M3
.venv/bin/python /tmp/om-l5-acceptance/arabic_subtitles.py    # worker 与 traced 帧一致
.venv/bin/python /tmp/om/m3_verify.py                         # 中/英/阿混合，0 缺字，硬件
.venv/bin/python /tmp/om/m3_chinese_driver.py after           # 既有中文路径，auto 与 cpu 帧一致
```

---

## 9. 提交

- 修复提交：`a6bbb27 fix(gpu): close the three blocking L5 findings (format, orphan, fonts)`
- 父提交（被阻断版本）：`585eed3 feat(gpu): PRoot-free native execution for solo hardware encodes`
- 变更文件：`lib/gpu/ffmpeg_args.py`、`lib/gpu/ffmpeg_exec.py`、`lib/gpu/oop.py`、`gpu_native/worker/om_gpu_worker.py`、`tests/lib/test_gpu_ffmpeg_args.py`、`tests/lib/test_gpu_dispatch_safety.py`
- **未推送、未合并。**
