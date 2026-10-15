# 独立 L5 验收三项阻断项的修复报告（M1 / M2 / M3）

日期：2026-10-14（第 1–2 轮）／2026-10-15（第 3 轮）
设备：Android / ARM64 / MediaTek MT6899（Mali-G720 MC8）/ Termux + PRoot Debian 12
分支：`feat/gpu-native-openmontage`（仅本地提交，未推送、未合并）
修复提交：`a6bbb27`（第 1 轮）　`205a370`（第 2 轮）　`3268e8e`（第 3 轮）　被阻断版本：`585eed3`

> 结论先行：独立 GPT-6.1 L5 验收给出的三个阻断项——**M1 未显式指定像素格式时的静默位深/色度降级**、**M2 硬件编码器孤儿与输出所有权（部分产物当成功发布的风险）**、**M3 免 PRoot 路径字体命名空间不等价导致字幕变方框且仍 `rc=0`**——已全部定位根因、修复并给出**前/后实测证据**。GPU 优先默认（`OM_GPU_MODE=auto`）、Mali/MediaTek 硬件路径与 CPU 回退均保持不变；未改动产品契约，未弱化既有测试。
>
> 第 3 轮（本页第 11 节）：最终 L5 复验判定 M2 仍阻断——第 2 轮的事务只覆盖 `gpu_plans`（被翻译成硬件编码的视频输出），同一命令里的 **stream copy / 音频 / 图片等其他文件输出仍直写生产路径**，实测把一份 20,897,137 字节成片截断为 0 字节。第 3 轮把「整条 FFmpeg 命令的输出契约」作为所有权单位，**无法安全接管时在改写任何文件之前明确失败**。
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

> **注意：** 本节记录**第一轮**修复（`a6bbb27`），只闭合了「硬件编码直接孤儿」。独立复审确认 **M2 仍阻断**（CPU 回退直接写生产路径、取消后回退子进程存活、多输出失败破坏既有资产）。第二轮补修见 **第 10 节**（提交 `205a370`）；最终复验指出第二轮事务仍只覆盖 `gpu_plans`，第三轮把所有权扩到**整条命令的全部文件输出**，见 **第 11 节**（提交 `3268e8e`）。本节结论以第 11 节为准。

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

- 第一轮修复提交：`a6bbb27 fix(gpu): close the three blocking L5 findings (format, orphan, fonts)`
  - 父提交（被阻断版本）：`585eed3 feat(gpu): PRoot-free native execution for solo hardware encodes`
  - 变更文件：`lib/gpu/ffmpeg_args.py`、`lib/gpu/ffmpeg_exec.py`、`lib/gpu/oop.py`、`gpu_native/worker/om_gpu_worker.py`、`tests/lib/test_gpu_ffmpeg_args.py`、`tests/lib/test_gpu_dispatch_safety.py`
- 第二轮（M2 复审补修）提交：`205a370 fix(gpu): stage the CPU fallback and own its lifecycle`（见第 10 节）
  - 变更文件：`lib/gpu/ffmpeg_exec.py`、`tests/lib/test_gpu_dispatch_safety.py`
- 第三轮（M2 全命令输出所有权）提交：`3268e8e fix(gpu): own every file output of an accelerated ffmpeg command`（见第 11 节）
  - 变更文件：`lib/gpu/ffmpeg_args.py`、`lib/gpu/ffmpeg_exec.py`、`tests/lib/test_gpu_dispatch_safety.py`、`tests/lib/test_gpu_ffmpeg_args.py`
- **未推送、未合并。**

---

## 10. M2 复审补修（第二轮）：CPU 回退的进程生命周期与产物所有权

第一轮修复（`a6bbb27`）闭合了「硬件编码直接孤儿」，独立复审确认 **M2 仍阻断**：*失败回退* 仍会破坏既有资产。本节记录第二轮的根因、修复、独立前/后复现与回归证据。

### 10.1 复审阻断项（三处已复现的 MAJOR 子例）

1. **取消 → CPU 回退继续写**：硬件编码被 `SIGKILL` → 进入 CPU 回退；回退命令**直接写最终路径**（带 `-y`），先把既有 `existing.mp4` 截断/覆盖；此时 dispatcher 收到 `SIGTERM`（`rc=-15`），CPU 子进程因**没有 `PR_SET_PDEATHSIG`、不在独立进程组**而**继续存活并写**，20,897,137 字节的既有产物被换成数十/数百字节。
2. **多输出中途失败**：第二个输出的父目录不存在时，CPU 回退直接写，先截断**已存在的第一个输出**，再在第二个上失败 → 第一个既有产物变成 **0 字节**。
3. **整组发布失败被部分提交**：第二个输出目标是目录时，`_commit_outputs` 逐个发布，发布完第一个后失败；`_remove_partial` 进一步把「已被改动」的第一个输出删除，随后 CPU 回退再次截断 → **0 字节**。

### 10.2 根因

`lib/gpu/ffmpeg_exec.py` 的 CPU 回退分支（原第 462 行）用 `subprocess.run(cpu_cmd, env=_cpu_env())` **直接运行原始未翻译命令**：无暂存、无独立会话、无 `PR_SET_PDEATHSIG`、无信号转发、无事务化发布。由此：

- 回退的写入者直接落在**生产路径**上，失败/取消即破坏既有资产；
- 回退子进程不随 dispatcher 退出而结束，成为「被遗弃的写入者」，可与后续重试并发写同一目标；
- `_commit_outputs` 逐项发布、**无回滚** → 部分发布被误当成可清理的中间态，进一步破坏旧产物。

### 10.3 修复：统一所有权模型（改动全部在 `lib/gpu/ffmpeg_exec.py`）

**同一套契约同时约束硬件尝试与 CPU 回退：**

1. **逐次尝试暂存**：新增 `_stage_cmd(cmd, targets, has_y)`，把每个非管道输出重定向到同目录唯一临时名 `.om-gpu-<pid>-<rand><ext>`；`_stage_outputs` 作为薄包装保留原签名（无法定位输出或「已有文件且无 `-y`」时抛 `_CannotStage` → 返回 `None`）。**硬件尝试与 CPU 回退各自使用全新的临时名**，两次尝试永不共享目的地。
2. **无法拥有则直接 exec**：若无法为必需的文件输出建立所有权（已有文件且无 `-y`、或输出 token 无法定位），不再运行「可回退的中间过程」，而是 `_exec(cpu_cmd)`——该进程**就是**唯一写入者，取消即直接杀死它，没有任何后续回退会与它竞争。`-y` 的覆盖确认语义原样保留。
3. **受控子进程**：新增 `_run_owned(cmd, env, capture_stderr)` = `start_new_session=True` + `preexec_fn=_set_pdeathsig`（`PR_SET_PDEATHSIG=SIGKILL`），由 dispatcher 统一登记活动进程组；硬件尝试与 CPU 回退都经它运行。
4. **取消感知**：加速段安装 `SIGTERM/SIGINT/SIGHUP` 处理器，把信号转发给**整个进程组**（`os.killpg`）并置取消标志；所有发布点先检查标志，一旦取消就**丢弃暂存、绝不发布**，随后 `_die_by_signal` 按原信号退出（退出状态与直接被信号终止一致）。重复信号升级为 `SIGKILL`。
5. **事务化发布 `_commit_outputs`**：先对**全部**目标预检（目标是目录 / 父目录缺失 / 父目录不可写）→ 命中即**不触碰任何文件**直接报错；再用硬链接（文件系统不支持时退化为 rename）为每个既有最终文件建立同 inode 备份，逐个 `os.replace` 发布并沿用原有权限位；任一步失败或期间被取消 → **回滚**（已发布者还原旧字节，未发布者的冗余备份仅删除），**绝不把部分发布报告为成功**。
6. **发布失败不触发 CPU 回退**：硬件尝试成功、仅发布阶段被拒（目标问题而非编码问题）时直接失败返回，**不再重跑一次 CPU 编码**（避免对同一目录再次动手）。
7. **远端→受追踪不共享临时名**：`_run_native_remote` 改为返回 `(attempted, result)`；远端「已启动但出错」时，受追踪兜底通过 `restage` 回调改用**全新临时名**，杜绝两个写入者共用一个输出路径。

`_snapshot` / `_remove_partial` / `_discard_outputs` 的签名与原有测试契约保持不变；`_remove_partial` 调用点保留为纵深防御（暂存后它已是安全空操作）。

### 10.4 独立前/后复现（真实子进程 + 真实视频输出）

用 `/tmp/om/m2r/repro.py` 复刻复审方三处用例（**同 argv、同故障注入、同断言**），仅把输出目录改到 `/tmp/om/m2r/<label>` 以免覆盖复审方证据。源素材 `assets/signal-from-tomorrow-demo.mp4`（1920×1080/30fps，20,897,137 字节，SHA-256 `47d46d…20dac`）。BEFORE = `git checkout HEAD -- lib/gpu/ffmpeg_exec.py`（`974c4f3`）；AFTER = 本轮补修。

| 用例 | 修复前 | 修复后 |
|---|---|---|
| dispatcher `SIGTERM`（硬件 SIGKILL→CPU 回退） | CPU 目标 = `existing.mp4`（**不是临时名**）；`cpu_survived_dispatcher_cancel=true`；20897137 → **48 字节**；`dispatcher_rc=-15`；旧资产**被破坏** | CPU 目标 = `.om-gpu-29660-9386aa6b.mp4`（**临时名**）；`cpu_survived_dispatcher_cancel=false`；20897137 不变；`dispatcher_rc=-15`；旧资产**逐字节保留** |
| dispatcher `SIGKILL`（处理器无法运行） | CPU 子进程**存活**并继续写；旧资产被破坏 | CPU 子进程随之**消失**（PDEATHSIG）；旧资产保留 |
| CPU 回退子进程被直接 `SIGTERM` | `rc=255`；20897137 → **2481 字节** | `rc=255`；20897137 **不变** |
| 多输出、第二个父目录缺失 | `rc=1`；既有第一个输出 → **0 字节** | `rc=1`；既有输出 **20897137 不变**；报 `cpu fallback produced no usable output` |
| 第二个目标是目录（发布被拒） | `rc=1`；既有输出 → **0 字节** | `rc=1`；既有输出 **不变**；报 `could not publish …: target is a directory`；**未再跑 CPU 回退** |
| 授权成功的 CPU 回退（硬件损坏、CPU 可修） | `rc=0`，产出有效视频 | `rc=0`，产出有效视频（1920×1080 h264，837,674 字节） |
| 取消后重试 | `rc=0`，产出有效 | `rc=0`，产出有效；无临时残留 |

SHA-256 对照：修复后上述**失败/取消**用例的既有产物 SHA-256 与原始值**完全一致**（`47d46d729e0881d37ea87e49a0d1cfb8277434bb58d66e0d51ac5163fd020dac`），即「失败 / 被取消的执行不改变既有有效产物」得到逐字节证明。

### 10.5 进程生命周期证据

- **生产实证**：修复后 CPU 回退的真实 argv 输出目标是**暂存名**——`/usr/bin/ffmpeg … /tmp/om/m2r/after/cancel/.om-gpu-29660-9386aa6b.mp4`，不是最终文件。
- 取消时 dispatcher 将 `SIGTERM` 转发给回退子进程的**整个进程组**，子进程在 1 秒内消失；`SIGKILL` dispatcher（处理器无法运行）时子进程仍因 `PR_SET_PDEATHSIG=SIGKILL` 立即终止 → **无被遗弃的写入者**。
- 单测 `test_owned_child_leads_its_session_and_carries_pdeathsig` 实测子进程 `sid==pid` 且 `PR_GET_PDEATHSIG==SIGKILL`；`test_owned_child_is_killed_when_its_dispatcher_dies` 用**真实子进程**在 dispatcher 被 `SIGKILL` 后验证子进程不存活。

### 10.6 回归测试

`tests/lib/test_gpu_dispatch_safety.py` 由 12 项扩到 **28 项**，新增覆盖复审指出未覆盖的三类：

- **暂存**：`test_stage_cmd_returns_pairs_for_a_cpu_command`、`test_stage_cmd_raises_when_it_cannot_own_the_destination`、`test_two_attempts_never_share_a_destination`
- **整组发布失败与回滚**：`test_commit_refuses_a_directory_target_before_touching_anything`、`test_commit_refuses_a_missing_output_directory`、`test_commit_rolls_back_when_a_later_publish_fails`、`test_commit_aborts_and_restores_when_the_job_was_cancelled`、`test_commit_keeps_the_preexisting_file_mode`
- **子进程生命周期**：`test_owned_child_leads_its_session_and_carries_pdeathsig`、`test_owned_child_is_killed_when_its_dispatcher_dies`
- **CPU 回退端到端**：`test_dispatch_cpu_fallback_stages_then_publishes`（回退经临时名发布、无残留、`backend=cpu`）、`test_dispatch_cpu_fallback_failure_leaves_the_old_asset`（回退失败绝不覆盖旧资产）、`test_dispatch_does_not_rerun_the_encoder_when_publish_is_refused`（发布被拒不重跑）、`test_dispatch_execs_directly_when_it_cannot_own_the_output`（无法拥有则直接 exec）
- **远端→受追踪不共享写入者**：`test_traced_fallback_restages_when_the_remote_attempt_errored`、`test_restage_is_skipped_when_no_remote_run_started`

结果：该文件 **28 passed**；GPU 四个定向模块（`test_gpu_oop` 14 + `test_gpu_ffmpeg_args` 54 + `test_gpu_dispatch_safety` 28 + `test_gpu_remotion_runner` 6）**102 passed**；全量 `pytest tests/ -q` **`2092 passed, 11 skipped, 3 xfailed`**（第一轮为 `2075 passed, 11 skipped, 3 xfailed`；本轮 `test_gpu_dispatch_safety.py` 由 12 项扩到 28 项）。

### 10.7 GPU 优先与回退回归

同一 1080p 素材（`-t 6 -an -c:v libx264 -crf 23 -preset medium -pix_fmt yuv420p`），`OM_GPU_MODE=auto`：

| 版本 | traced 中位数 | 硬件输出 SHA-256 | backend / encoder | worker 路径 |
|---|---|---|---|---|
| 修复前（`974c4f3`） | 5.10 s | `c126338d…d1ad` | gpu / `h264_mediacodec` | 2.24 s，SHA 相同，`oop=true` |
| 修复后 | 4.44 s | `c126338d…d1ad` | gpu / `h264_mediacodec` | 2.08 s，SHA 相同，`oop=true` |

硬件输出**逐字节一致**，仍为 `c2.mtk.avc.encoder`、`backend=gpu`；差异在噪声范围内（本轮改动只在启动/收尾路径，不进入逐帧编码循环）。

**流式/管道路径回归**（该路径的子进程现在同样经 `_run_owned` 独立会话运行）：`ffmpeg … -f mp4 … pipe:1 > out.mp4`（`writes_stdout`）与 `cat in.mp4 | ffmpeg -i pipe:0 …`（`reads_stdin`）均 `rc=0`、输出为有效 1920×1080 h264、遥测 `backend=gpu, streaming=true, encoder=h264_mediacodec` 保持不变。

### 10.8 剩余局限

1. dispatcher 被 `SIGKILL`（或工具级超时强杀）时无法运行清理，可能留下一个 `.om-gpu-*` 暂存文件；**它不会是生产文件、也没有任何写入者存活**（本次实测 `leftover_temps` 仅此一个，且只见于 `SIGKILL` 用例）。属可接受的、非破坏性残留。
2. `PR_SET_PDEATHSIG` 只绑定**直接子进程**（与复审方观察一致）；本次未发现实际 FFmpeg 派生的编码孙进程，故未对「泛用 Python 子孙边界」做额外处理。
3. 取消收敛依赖 FFmpeg 对 `SIGTERM` 的正常退出；若某编码器忽略 `SIGTERM`，重复信号会升级为 `SIGKILL`，但首次取消的收敛时间由编码器决定。
4. GPU 流式/管道输出无暂存与回退（设计如此）；其子进程存活由 `PR_SET_PDEATHSIG` 绑定。
5. 本报告**不构成独立 L5 通过结论**；M1/M3 维持第一轮结论不变。

### 10.9 复现步骤

```bash
# 前/后（同一脚本，仅输出目录不同）
git checkout HEAD -- lib/gpu/ffmpeg_exec.py
python3 /tmp/om/m2r/repro.py before           # 复现三处破坏
cp /tmp/om/m2r/ffmpeg_exec.fixed.py lib/gpu/ffmpeg_exec.py
python3 /tmp/om/m2r/repro.py after            # 全程保留旧资产

# GPU 优先 / 性能 A/B
python3 /tmp/om/m2r/perf.py fixed             # backend=gpu, SHA=c126338d…
git checkout HEAD -- lib/gpu/ffmpeg_exec.py && python3 /tmp/om/m2r/perf.py base
cp /tmp/om/m2r/ffmpeg_exec.fixed.py lib/gpu/ffmpeg_exec.py

# 回归
.venv/bin/python -m pytest tests/lib/test_gpu_dispatch_safety.py -q
```

### 10.10 提交

- 补修提交：`205a370 fix(gpu): stage the CPU fallback and own its lifecycle`
- 变更文件：`lib/gpu/ffmpeg_exec.py`、`tests/lib/test_gpu_dispatch_safety.py`
- **未推送、未合并。**

---

## 11. M2 复审补修（第三轮）：整条 FFmpeg 命令的输出所有权

最终 L5 复验（`/tmp/om-l5-m2-final/REPORT.zh-CN.md`）判定 M2 仍阻断：第 2 轮的事务只覆盖 `gpu_plans`，同一命令中的**其他文件输出**仍可绕过保护。本节记录第三轮的根因、更正后的所有权模型、以**真实子进程与真实 FFmpeg** 做的独立前/后复现、SHA-256 与进程生命周期证据、回归与 GPU 优先验证、以及剩余限制。

### 11.1 根因与更正后的所有权模型

**根因（第 2 轮代码 `eae7e49`）**——`lib/gpu/ffmpeg_exec.py:668` 与 `:726` 附近：

```python
file_outputs = [p.target for p in gpu_plans if not ffmpeg_args.is_pipe(p.target)]
staged = _stage_outputs(native_cmd, gpu_plans)   # 硬件尝试
cpu_staged = _stage_outputs(cpu_cmd, gpu_plans)  # CPU 回退
```

- 被暂存的目标只来自 `gpu_plans`（被翻译成 `h264_mediacodec/hevc_mediacodec` 的视频输出）。
- 同一命令里的 **stream copy（`-c copy`）、音频、图片** 等输出属于 `OutputPlan.action == "none"`，其 target 仍**逐字保留**在命令里 → 没有暂存、没有校验、没有回滚。
- `_stage_outputs` 在无法接管时**吞掉原因**并返回 `None`，调用方一律 `_exec(cpu_cmd)`：对「目标 token 找不到」这种**无法识别**的情形也退化成未保护的直写。
- `_validate` 硬性要求 `v:0` 视频流，无法校验音频/stream copy/图片输出。
- 实测破坏：GPU 视频输出指向不存在的目录时，硬件尝试与 CPU 回退**先直接截断**同一命令里已存在的 `existing.mp4`（20,897,137 字节 → 0 字节）与 `audio.m4a`（18,784 字节 → 0 字节），再在第二个输出上失败。

**更正后的所有权模型**——把「**整条命令的输出契约**」作为所有权单位，新增 `lib.gpu.ffmpeg_args.own_outputs()` 先对命令的**全部输出**分类（`OutputOwnership.kind`）：

| kind | 判定 | 处置 |
|---|---|---|
| `file` | 单一路径文件（视频编码 / stream copy / 音频 / 图片 / 校验和文件） | 每次尝试暂存到同目录临时文件 → 按媒体类型校验 → 一次性发布 → 失败整体回滚 |
| `pipe` | `-`、`pipe:`、`fd:` | 保留普通流式语义，**不暂存** |
| `other` | `-f null`、网络端点（`rtmp://…`）、已存在的设备/FIFO/socket 等非普通文件 | 从不暂存、从不发布 |
| `unsafe` | **会写文件但无法用单一目标接管**：含 `%` 的编号 pattern，或 `segment/hls/dash/tee/webm_segment` 等多文件 muxer | **在任何文件被改写之前拒绝整条命令（`rc=78`）** |

流程（`dispatch`）：分类 → 若有 `unsafe` 立即拒绝 → 把全部 `file` 目标交给 `_stage_cmd`（**硬件尝试、CPU 回退、流式命令三处都走同一 `file_outputs` 集合**）→ 校验（视频要求带尺寸的 `v:0`；音频/图片/stream copy 要求至少一条流；文本/校验和只要求非空）→ `_commit_outputs` 事务发布（预检目录/父目录/可写 → 硬链接备份 → `os.replace` → 失败/取消整体回滚）。

- **无法安全接管即失败**：`unsafe` 目标、或在命令行中定位不到的输出 token，都在**任何进程启动之前**以 `rc=78` 明确失败并写 telemetry（`backend=gpu-failed`）。
- **保留 ffmpeg 自身语义**：仅当「目标已存在且命令没有 `-y`」时仍 `_exec(cpu_cmd)`——此时 ffmpeg 自己会拒绝写入，不产生未保护写；`-y` 授权的正常覆盖语义不变。
- **多文件 muxer 不再翻译**：`plan_output` 对 `_MULTIFILE_MUXERS` 返回 `none`，因此**纯 HLS/segment 命令不再被加速**，回到原有的未加速直通路径（不触发拒绝）；只有「GPU 视频输出 + 无法接管的文件输出」混合命令才会明确拒绝。
- **多输出整体失败语义不变**：`_commit_outputs` 的预检/备份/回滚保证「多输出失败保留既有有效产物，绝不把部分发布当成功」，且**发布失败不会再跑 CPU 回退**。

### 11.2 变更文件与提交

- 实现 + 测试提交：`3268e8e fix(gpu): own every file output of an accelerated ffmpeg command`
  - `lib/gpu/ffmpeg_args.py`：新增 `_MULTIFILE_MUXERS` / `_NONFILE_MUXERS` / `_TEXT_MUXERS` / `_SCHEME_RE`；`plan_output` 对多文件 muxer 返回 `none`；新增 `OutputOwnership` / `output_ownership()` / `own_outputs()`。
  - `lib/gpu/ffmpeg_exec.py`：`_validate` 改为按 `(path, kind)` 的媒体类型校验；`_CannotStage` 增加 `kind`（`overwrite` / `absent`）；删除会吞掉原因的 `_stage_outputs`，新增 `_unowned_staging()`；`dispatch` 以 `own_outputs` 为所有权面，硬件尝试 / CPU 回退 / 流式分支统一暂存并发布；模块 docstring 同步。
  - `tests/lib/test_gpu_dispatch_safety.py`：28 → 41 个用例（混合失败/成功、流式暂存、拒绝模式与多文件 muxer、无法定位、覆盖拒绝直通、媒体类型校验）。
  - `tests/lib/test_gpu_ffmpeg_args.py`：新增 8 组所有权分类用例；多文件 muxer 加入「不翻译」参数表。
- 未推送、未合并；工作区干净。

### 11.3 独立前/后复现（混合输出）

复现脚本自建（不覆盖复审方证据目录）：`/tmp/om/m2f/harness.py`，结果 `/tmp/om/m2f/{before,after}/results.json`。

- **before 树** = `git show HEAD:lib/gpu/{ffmpeg_exec,ffmpeg_args}.py`（`eae7e49`，第 2 轮版本）落在私有 repo 根 `/tmp/om/m2f/before`，经真实 shim `gpu_native/shims/path/ffmpeg` 运行；**after 树** = 工作区（`3268e8e`）。
- **两种运行时画像**，各自使用**私有 `OM_GPU_HOME`**（真实设备状态不被触碰）：
  - `stub`：native ffmpeg 先照抄真实 MediaCodec 编码器清单，再对任何编码**必然失败** → 用真实子进程确定性地产生「硬件失败 → CPU 回退」；
  - `real`：符号链接真实 native 运行时（`prefix/`、`bin/`、`manifest.json`）→ 真实硬件编码。
- CPU ffmpeg 用日志包装脚本（`OM_GPU_REAL_FFMPEG`）逐次记录调用，native 桩同样记录 → 可精确核对尝试次数。

**用例 1（复验方确认的混合输出失败）**

```
ffmpeg -v error -y -i <src> -t 1 -c copy existing.mp4 \
       -t 1 -vn -c:a aac audio.m4a \
       -t 1 -an -c:v libx264 -pix_fmt yuv420p missing/second.mp4
```

| | rc | `existing.mp4` | `audio.m4a` | `missing/` | 残留临时文件 |
|---|---|---|---|---|---|
| **before** | 1 | 20,897,137 B → **0 B**（SHA `47d46d72…` → `e3b0c442…`） | 18,784 B → **0 B** | 未创建 | 无 |
| **after** | 1 | 20,897,137 B → **20,897,137 B，SHA 不变** | 18,784 B → **18,784 B，SHA 不变** | 未创建 | 无 |

**用例 2–8** 汇总（`after` 未回归的一并列出）：

| 用例 | before | after |
|---|---|---|
| 混合成功（copy+音频+GPU 视频，均授权覆盖） | rc=0，三产物全发布 | rc=0，三产物全发布；stream copy/音频**逐字节相同**（copy SHA `bde5489a…`、audio `0f69ee4a…`），视频 h264 1920×1080、音频 aac |
| 取消（SIGTERM 命中 CPU 回退中） | rc=-15；`existing.mp4` 被改成 **4,048,620 B**（SHA `f6512d72…`，内容损坏） | rc=-15；`existing.mp4` **SHA 不变**；无存活写入者、无残留临时文件 |
| 多输出失败（两个 GPU 输出，第二个目录缺失） | rc=1；`a.mp4` 保留 | rc=1；`a.mp4` 保留（第 2 轮行为，无回归） |
| 发布被拒（目标为目录，真实硬件成功） | rc=1；`good.mp4` **被覆盖** | rc=1；`good.mp4` **保持原尺寸**；`blocked.mp4` 仍为目录；`backend=gpu-failed`；CPU 侧仅有 `-hide_banner -version` 探测，**未重跑编码** |
| 流式（写 `pipe:1` + 文件输出） | rc=0；stdout 761,024 B；`copy.mp4` 1,598,286 B | rc=0；stdout **761,024 B**、mpegts h264；`copy.mp4` **SHA 相同**（`739c902e…`）；文件输出已暂存发布 |
| 含编号 pattern 的混合命令 | rc=0，写出 `out.mp4` 与 `frame_0001/0002.png` | **rc=78**，`nothing was written`（目录内无任何产物） |
| GPU 优先真实硬件编码 | rc=0；SHA `4db4336c…`；`backend=gpu`、`h264_mediacodec` | rc=0；**SHA 完全相同**；`backend=gpu`、`h264_mediacodec`、`c2.mtk.avc.encoder`；CPU 侧**零调用** |

### 11.4 既有资产 SHA-256 校验

- 失败/取消用例中，既有产物在 `after` 全部保持原 SHA-256：
  - `existing.mp4`：`47d46d729e0881d37ea87e49a0d1cfb8277434bb58d66e0d51ac5163fd020dac`（20,897,137 B，前=后）
  - `audio.m4a`：`484781420609973d9a7f061c96d9800a8df7a6a34990a4bb3c1986afaadab331`（18,784 B，前=后）
  - 取消用例 `existing.mp4` 同为 `47d46d72…`（`before` 被改成 `f6512d72…` 且尺寸缩水）
  - 发布被拒用例 `good.mp4` 尺寸与内容均未变
- 成功用例中授权覆盖语义不变，多产物**同一事务**发布，无部分发布。
- 设备真实 `OM_GPU_HOME` 未被写入；所有复现均在私有 `OM_GPU_HOME`（`/tmp/om/m2f/home-*`）与 `/tmp` 沙箱内完成。

### 11.5 进程生命周期与发布证据

- 取消用例 `after`：信号前活跃写入者只写**暂存路径**（`/usr/bin/ffmpeg … .om-gpu-<pid>-<hex>.mp4`）；`writers_after=[]`（无 ffmpeg 存活）、`leftovers=[]`（无残留暂存）；既有产物 SHA 不变 → 「取消不得发布、不得留下写入者」成立。
- 发布被拒用例：CPU 侧日志只有 `-hide_banner -version`（参考实现语义探测），**没有**第二次编码 → 「发布失败不得再跑 CPU 回退」成立。
- 全部用例的产物目录在结束后都不含 `.om-gpu-*` 临时文件或 `.bak`。
- 硬件尝试与 CPU 回退仍各自 `start_new_session` + `PR_SET_PDEATHSIG`（第 2 轮成果，未改动）。

### 11.6 回归测试

- 4 个 GPU 模块：**127 passed**（`test_gpu_oop` 14 + `test_gpu_ffmpeg_args` 54 + `test_gpu_dispatch_safety` 41 + `test_gpu_remotion_runner` 6；前值 102）。
- 全量：`.venv/bin/python -m pytest tests/ -q` → **2117 passed, 11 skipped, 3 xfailed**（EXIT=0；前值 2092 passed，增量 25 即本轮新增用例）。
- 既有测试未弱化：`_stage_outputs` 的用例改写为直接断言 `_stage_cmd` 的新契约（覆盖拒绝 → `kind == "overwrite"`；token 缺失 → `kind == "absent"`），语义更严格；`test_dispatch_execs_directly_when_it_cannot_own_the_output` 拆分为「无法定位 → 拒绝」与「ffmpeg 自身拒绝覆盖 → 直通」两个更强的用例。

### 11.7 GPU 优先与回退兼容

- GPU 优先默认（`OM_GPU_MODE=auto`）与 Mali/MediaTek 硬件路径不变：真实硬件编码输出 SHA-256 前/后**完全一致**（`4db4336c…`），telemetry `backend=gpu`、`encoder=h264_mediacodec`、`device=c2.mtk.avc.encoder`；该次运行 CPU 侧**零调用**。
- CPU 回退仍按第 2 轮语义工作：确定性构造「硬件失败 → CPU 回退成功」时全链路 rc=0、产物按媒体类型校验后发布（用例「混合成功」）。
- 流式/管道语义保持：`pipe:1` 输出的**字节数与内容不变**；文件输出在流式命令中也被暂存并发布（管道本身不暂存）。
- 未加速命令（stream copy/分析/音频/图片/多文件 muxer 单一输出）仍走原有的 CPU 直通快路径，shim 只做一次解析。

### 11.8 剩余限制

1. **新增的拒绝语义（刻意收紧）**：当一条**被加速**的命令同时含有无法接管的文件输出（编号 pattern 或多文件 muxer）时，命令以 `rc=78` 明确失败而不是继续执行未保护的直写。仓库现有工具不存在这种组合；纯 HLS/segment 命令因多文件 muxer 不再翻译而**照旧在 CPU 直通**，不受影响。
2. 流式（读 stdin / 写 stdout）命令的文件输出现在也暂存与发布；其管道输出按定义不暂存、无回退。
3. `-f null`、网络端点、设备/非普通文件目标不参与暂存（不视为可接管的文件输出），只保留其原有语义。
4. 沿用第 2 轮限制：被 `SIGKILL` 的 dispatcher 无法运行清理，可能留下一个**非生产**暂存文件（无写入者存活）；`PR_SET_PDEATHSIG` 只绑定直接子进程；取消收敛时间取决于编码器对 `SIGTERM` 的响应。
5. 「目标已存在且无 `-y`」仍直接 `_exec` 原命令（保留 ffmpeg 自身拒绝/提示语义）。
6. **本报告不构成独立 L5 通过结论**；M1/M3 维持前两轮结论不变。

### 11.9 复现步骤

```bash
# 前/后（同一脚本，脚本自建私有 repo 根与私有 OM_GPU_HOME，不写设备状态）
python3 /tmp/om/m2f/harness.py before     # 复现截断/损坏
python3 /tmp/om/m2f/harness.py after      # 全程保留既有资产
# 逐用例对比
python3 - <<'PY'
import json
b=json.load(open('/tmp/om/m2f/before/results.json'))
a=json.load(open('/tmp/om/m2f/after/results.json'))
print(b['mixed_fail']['asset_preserved'], a['mixed_fail']['asset_preserved'])
PY

# 回归
.venv/bin/python -m pytest tests/lib/test_gpu_ffmpeg_args.py tests/lib/test_gpu_dispatch_safety.py -q
.venv/bin/python -m pytest tests/ -q
```

### 11.10 提交

- 补修提交：`3268e8e fix(gpu): own every file output of an accelerated ffmpeg command`
- 变更文件：`lib/gpu/ffmpeg_args.py`、`lib/gpu/ffmpeg_exec.py`、`tests/lib/test_gpu_dispatch_safety.py`、`tests/lib/test_gpu_ffmpeg_args.py`
- **未推送、未合并。**
