# 全域 L5 确认缺陷 F1 / F2 的修复报告

日期：2026-10-15
设备：Android / ARM64 / MediaTek MT6899（Mali-G720 MC8）/ Termux + PRoot Debian 12
分支：`feat/gpu-native-openmontage`（仅本地提交，未推送、未合并）
被验收版本：`734c455`　修复提交：``514d1e5`（调度器：F1-a/F1-b/F2、拥有的 CPU 直通、断连重试）、`815a80a`（交付边界：F1-c、compose、HyperFrames、音频 mux）、本报告提交`

> 结论先行：独立全域 L5 验收确认的两个 MAJOR——**F1 生产输出所有权不完整**（F1-a 本地 `file:` URL、F1-b 隐式 HLS 与分片、F1-c Remotion 最终封装；FFmpeg compose 交付路径同类暴露）和 **F2 FFmpeg 全流 codec 与视频 codec 的选项顺序/作用域被改变**——已在当前实现上逐条重新复现、定位根因并修复，给出前/后实测证据（含 SHA-256 与 ffprobe）。GPU 优先默认（`OM_GPU_MODE=auto`）、Mali 图形/计算、MediaTek 硬件编码、Remotion 渲染与 CPU 回退均保持；此前 M1/M3 的修复未被改动；未弱化任何既有测试。
>
> **本报告不主张独立 L5 通过。** 最终验收归 GPT 6.1 Sol Ultra；下列复现应由其按自身用例重跑。

---

## 0. 依据、范围与边界

**依据。** 任务指定的总报告 `/tmp/om-l5-full-acceptance/REPORT.zh-CN.md` 在接手时并不存在于磁盘；实际使用同目录下的三份子域报告（`semantics/REPORT.md`、`render/REPORT.md`、`runtime/REPORT.md`）、`ownership/results.json`、`ownership_probe.py`、`semantics/probe.py`、`semantics/hls_probe.py`、`render/mux_cancel.py` 及其证据，并在**当前**代码上全部重新复现（见 §3，修复前的结果与验收方一致）。

**只修 F1、F2。** 未触碰：Android 权限/服务/安全设置、内核、GPU 驱动、PRoot 基础设施、其他应用；未推送、未合并；未删除、未重建任何工具链（设备上的原生运行时 `~/.openmontage-gpu` 仅被只读符号链接引用，所有可变状态在私有 `OM_GPU_HOME`）。

**验收方子域报告中的 Partial 项（`runtime/REPORT.md` 的「断链后重试」）**：因直接对应本任务第 4 条证据要求（弃用写入者不得在回退/取消后破坏目的地），一并以最小改动关闭（§1.7）。

---

## 1. F1：生产输出所有权

### 1.1 根因（均指向 `734c455` 的源码）

| 缺陷 | 位置 | 机理 |
|---|---|---|
| F1-a | `lib/gpu/ffmpeg_args.py` `output_ownership`（`_SCHEME_RE = ^[A-Za-z][…]*://`） | `file:///…/existing.mp4` 匹配 `://` 而被归为 `kind="other"`（网络端点）→ 既不暂存也不发布，硬件/CPU 写入者**直接拿到生产路径**；失败/取消即把既有视频截断。无 `//` 的 `file:rel.mp4` 反而被当作名为 `file:rel.mp4` 的普通文件，发布时会写出名字里带冒号的错误文件。 |
| F1-b | 同上 `_container` / `output_ownership` | 只看显式 `-f`；`playlist.m3u8` 不被识别为 HLS → 被当作单文件输出暂存，而 `-hls_segment_filename` 原样透传，分片以**生产文件名**直接写入；整体命令随后失败时，既有 `.ts` 已被覆盖。 |
| F1-c（1） | `lib/gpu/ffmpeg_exec.py` `dispatch` | 「无可加速内容」的命令（含 Remotion 最终 `-c:v copy -c:a copy -y final.mp4`）走零开销 `_exec`，**原样直写目的地**：失败/取消 = 截断。 |
| F1-c（2） | `lib/gpu/remotion.py` `run_render` | 渲染边界没有所有权：降级的 GPU 尝试先把（可能有缺陷的）成片写到目的地，随后 CPU 重渲若失败/被取消，既有成片已丢；后续的外部音频 mux 失败同理（成片已被无音频版本替换）。 |
| compose | `tools/video/video_compose.py` | `_compose` 最后一条 FFmpeg 命令（及 `burn_subtitles`/`overlay`/`encode`）直接写 `output_path`。 |

### 1.2 修复总览

1. **按 FFmpeg 实际打开方式解析目的地**（`ffmpeg_args.url_scheme/local_path/write_target`）：协议判定同 `url_find_protocol`（前导 scheme 字符 + `:`，无需 `//`）；`file:` 前缀按 FFmpeg `file` 协议剥离（不做百分号解码、无特殊 host；`file:///a`→`/a`，`file:rel`→`rel`，`FILE:x` 是不存在的协议）；符号链接跟随到真正被写入的文件并在其旁暂存。
2. **有效 muxer**（`effective_muxer`）：显式 `-f`，否则按扩展推断多文件 muxer（`.m3u8`→hls、`.mpd`→dash、`.chk`→webm_chunk）；显式与隐式**等价**。`smoothstreaming`/`hds` 并入多文件集合。
3. **sidecar/多文件**：写额外媒体文件的选项（`hls_segment_filename`、`segment_list`、`init_seg_name`、`media_seg_name`、`master_pl_name`、`hls_fmp4_init_filename`、`var_stream_map`、`single_file_name`）与写本地文件的协议（`tee:`、`md5:`、`crypto*`）使该输出**不可接管**；同一目的地的两种写法（`a.mp4` 与 `file:a.mp4`、符号链接与其目标）和「输出同时是输入」（FFmpeg 自己会拒绝的原地编辑）也**不可接管**。被加速的命令含不可接管输出 → 在写任何文件之前 `rc=78` 拒绝（既有语义）；诊断类日志选项（`-progress`、`-vstats_file`、`-report`、`-passlogfile`）不是生产媒体资产，保持透传。
4. **无可加速内容的命令也不得毁掉生产文件**（`_run_cpu_owned`/`_passthrough`）：命令带 `-y` 且会覆盖一个**已存在的普通文件**时，原命令（逐字不改）作为被拥有的子进程在 CPU FFmpeg 上运行，所有文件输出写到同目录的隐藏暂存名，**成功后**才整体原子发布；失败、被杀、取消均保留原文件。stdin/stdout/stderr 直通，退出状态/致命信号透传；目的地不存在时仍是零开销 `exec`；无法接管（目录不可写、令牌无法定位）时回到原样 `exec`。
5. **渲染/合成边界**（新模块 `lib/gpu/delivery.py`）：当渲染将覆盖一个**已存在**的文件时，每个尝试（GPU、其后的 CPU 重渲）各自渲染到独立的隐藏暂存名，仅把**成功且校验通过**的那一次原子发布到目的地。接入点：`remotion.run_render(output=…)`（`VideoCompose` 两条 Remotion 路径与 `RemotionCaptionBurn`）、`VideoCompose._run_ffmpeg_to`（compose 最终步骤与 `burn_subtitles`/`overlay`/`encode`）、`HyperFramesCompose` 的两处 `render`。
6. **多步交付**：外部「已批准混音」的 mux 作为同一次交付的 `finalize` 步骤在**发布之前**对暂存成片执行（`run_render(finalize=…)`），其失败与渲染失败一样不会替换既有成片，且错误仍按「音频 mux 失败」而非「渲染失败」上报。
7. **失联不等于没运行**（`lib/gpu/oop.py`）：请求已发出后连接断开，原来返回 `None`（与「没有 worker」同形），调度器据此复用同一暂存名；现在返回带错误的结果，调度器改用全新暂存名（`restage`），避免两个写入者共享一个暂存路径。

### 1.3 关键设计约束（避免过度设计）

* **只在「有既有资产需要保护」时介入。** 渲染目的地不存在时直接写（与此前完全一致），因此 Remotion/合成命令行里的输出路径、四个**已批准**的单元测试对「命令里出现最终输出路径」的断言均无需改动。需要保护的情形（目的地已存在）才暂存、校验、发布。
* **取消 = 不发布。** 与 M2 既有语义一致；被 `q` 正常结束（子进程自行 rc=0）会发布，被 SIGTERM/SIGINT 取消则丢弃暂存并按该信号退出。
* **不引入新的跨文件事务机制**：复用既有的 `_commit_outputs`（硬链接备份、整体发布、失败回滚、保留权限位）。
* **磁盘空间权衡**：覆盖大文件时，旧文件与暂存同时存在，峰值占用多一个旧文件大小；空间不足时命令失败但**旧资产保留**（此前是先截断旧文件再失败）。

---

## 2. F2：FFmpeg 选项作用域与顺序

### 2.1 根因

FFmpeg 对每条输出流逐一按**命令行顺序**扫描选项，取**最后一个**说明符匹配该流的选项（对不同说明符的混用同样如此）：`-c copy -c:v libx265` 重编码视频、其余流复制；`-c:v libx265 -c copy` 则视频被复制。

`ffmpeg_args.plan_output` 却写成 `_last(-c:v 家族) or _last(-c/-codec)`（先看视频专属再看通用，**与顺序无关**）；`translate` 删去 `-c:v` 与值为 `libx264…` 的 `-c`，再把新的 `-c:v hevc_mediacodec` **插到最前面**，被保留的 `-c copy` 排在其后并覆盖了它——硬件编码器从未真正执行，输出成了未转换的复制品（验收方复现：`H.264 High 4:4:4 Predictive / yuv444p10le`），而 `_validate` 只要有尺寸就放行，遥测按「计划」冒报 `backend=gpu, hevc_mediacodec`。

### 2.2 修复

1. **按 FFmpeg 规则解析有效编码器**：`_canon` 归一化选项名（`-vcodec`/`-acodec`/`-codec:v`/`-vb`…）与说明符；`_scoped_last` 取**最后一个**作用于视频流的选项（无说明符或 `v`/`V`；`v:0` 仅在输出必为单个视频流——无 `-map` 且无 `-filter_complex` ——时等价于 `v`）；`a/s/d/t` 类型说明符不触及视频；其余说明符（`0`、`#id`、`m:…`、`disp:…`、`v:1`…）**无法静态判定 → 保留原始 CPU 命令**。同一规则用于 `-pix_fmt`、`-profile`、`-crf`、`-qp`、`-g`、`-r`、`-b`、`-force_key_frames`；`-pass`/`-passlogfile`（两遍编码，硬件编码器无法复现）一律保留 CPU。
2. **就地改写**：翻译只把「选中编码器的那个选项」的**值**换成 `*_mediacodec`，键（作用域）与位置不变；隐式编码器才显式追加 `-c:v`。码率/GOP/profile 等硬件参数位置无关，仍置前。
3. **发布前验证实际产物**（`ffmpeg_exec._validate`）：硬件编码输出的**每个**视频流（忽略封面图）必须是所请求的 codec 家族且为 8-bit 4:2:0（`yuv420p`/`yuvj420p`）；不符 → 视为失败的硬件尝试：`auto` 丢弃暂存并重跑**原始** CPU 命令，`require` 报失败，**均不发布**。
4. **遥测与真实执行一致**：成功事件带 `verified: {路径: "hevc/yuv420p"}`（来自 ffprobe 对已发布文件的实测）、多编码器/多设备时列出全部（`h264_mediacodec,hevc_mediacodec` / `c2.mtk.avc.encoder,c2.mtk.hevc.encoder`）、同命令中仍由软件编码的输出列入 `software_outputs`；验证失败的 GPU 尝试记为 `cpu`（或 `gpu-failed`）并带 `output contract mismatch` 原因；`telemetry.summarize` 把这些字段带进 `ToolResult.data["gpu_execution"]`。

### 2.3 对旧实现的广度证据

对 20 种 `-c`/`-c:v`/`-vcodec`/`-codec:v`/`-c:v:0`/`-c:s` 的顺序组合，以**真实 FFmpeg** 为裁判（`tests/lib/test_gpu_option_scoping.py`）：旧实现在 **12/20** 组合上给出错误的计划或错误的翻译（含 `-c:v libx264 -c copy`：旧实现计划为硬件编码，实际是视频复制），新实现 **20/20 一致**。

---

## 3. 前/后独立复现

复现脚本与全部结果：`/tmp/om-f1f2-remediation/evidence/`（`{f1a,f1b,f1c,f2,compose_live,compose_fault,perf,remotion_perf}.py`，共享 `common.py`；每个场景使用私有 `OM_GPU_HOME`，运行时二进制只读符号链接，设备真实状态不被写入；`before/`、`after/` 下是各场景的 `result.json`/`results.json`/遥测 `exec.jsonl`，`TABLES.md` 是汇总）。修复前运行于 `734c455`，修复后运行于工作区。全量测试日志：`/tmp/om-f1f2-remediation/full-suite-final.log`。

### 3.1 F1-a：本地 `file:` URL（`f1a.py`）

命令（混合失败，与验收方 `ownership_probe.py` 同形；第二个输出的目录不存在，保证命令在第一个输出已被打开之后失败）：

```
ffmpeg -v error -y -i assets/signal-from-tomorrow-demo.mp4 -t 1 -c copy file:///…/existing.mp4 \
       -t 1 -an -c:v libx264 -pix_fmt yuv420p …/missing/second.mp4
```

既有资产：`existing.mp4`，20,897,137 B，SHA-256 `47d46d729e0881d37ea87e49a0d1cfb8277434bb58d66e0d51ac5163fd020dac`（空文件 SHA 为 `e3b0c442…b855`）。

| 用例 | 修复前（`734c455`） | 修复后 |
|---|---|---|
| 普通路径·混合失败（对照） | rc=1，SHA `47d46d72…` 不变 | rc=1，SHA 不变 |
| **`file://` 路径·混合失败** | rc=1，**→ 0 B**（`e3b0c442…`） | rc=1，**20,897,137 B，SHA `47d46d72…` 不变**，无残留 |
| `file://` 路径·单输出失败（坏音频编码器） | rc=1，不变（失败发生在打开输出之前） | rc=1，不变 |
| **`file://` 路径·取消 SIGTERM**（`-re` 编码中途） | rc=-15，**→ 374,944 B**（`88ad6d61…`，内容损坏） | rc=-15，**SHA 不变**，无残留 |
| **`file://` 路径·取消 SIGKILL** | rc=-9，**→ 0 B** | rc=-9，**SHA 不变**；仅留一个隐藏暂存文件（写入者已随调度器死亡，见 §7-1） |
| 普通路径·取消 SIGTERM（对照） | rc=-15，SHA 不变 | rc=-15，SHA 不变 |
| `file://` 授权覆盖（成功） | rc=0，→ 756,772 B `29d0e66c…` | rc=0，→ 756,772 B `29d0e66c…`（**逐字节相同**），h264/yuv420p + aac，无残留 |
| 相对 `file:rel.mp4` | 发布时会写出名为 `file:rel.mp4` 的文件 | 写出 `rel.mp4`，不存在名字带冒号的文件 |
| 失败后用同一路径重试 | — | 失败 rc=1 且资产不变；重试 rc=0，h264/yuv420p + aac，目录只剩 `existing.mp4` |

### 3.2 F1-b：隐式 HLS 与 sidecar（`f1b.py`）

先制造有效分片 `production_000.ts`（54,896 B，SHA `f95384b587900d6678ad2179169bf9d72c209e1752ce61c777105474dbcd950a`），再运行第一输出为 `playlist.m3u8`（未写 `-f hls`，`-hls_segment_filename production_%03d.ts`）、第二输出为已存在的**目录** `blocked.mp4` 的命令（预检在编码完成后失败）。

| 用例 | 修复前 | 修复后 |
|---|---|---|
| **隐式 HLS + 被阻塞的第二输出** | rc=1；`production_000.ts` **→ 36,472 B**（SHA `5051348a…`），另写出 `production_001.ts` | **rc=78**（`refusing to run … nothing was written`）；分片 SHA `f95384b5…` 不变；目录无新文件 |
| 显式 `-f hls` 同形命令 | rc=78，不变 | rc=78，不变（**显式与隐式现在等价**） |
| 隐式 DASH（`manifest.mpd`）同形命令 | rc=1，分片未被覆盖但留下 `init-stream0.m4s`、`chunk-stream0-00001.m4s` 两个生产命名的新文件 | rc=78，目录无新文件 |
| sidecar 选项（`-segment_list …`）同形命令 | rc=1，发布被拒 | rc=78，目录无新文件 |
| **纯**隐式 HLS（无第二输出、无加速内容） | rc=0，写出 playlist+分片 | rc=0，写出 playlist+分片（**未被拒绝，行为不变**） |

### 3.3 F1-c：Remotion 最终封装（`f1c.py`，真实 GPU 渲染）

在私有目录里放一份有效成片（同一 20,897,137 B / `47d46d72…`），用真实的 `VideoCompose.execute(remotion_render)` 渲染到该路径（`OM_GPU_MODE=require`，真实 Mali Chromium + MediaTek 硬件预编码）。注入器放在 Remotion `--binaries-directory` 的 `ffmpeg` 位置：对**最终交付** mux 加 `-re` 放慢读取，并在目标目录出现写入（生产文件尺寸变化，或同目录新出现暂存文件）的瞬间向该 FFmpeg 进程组发 `SIGKILL`。

| | 修复前 | 修复后 |
|---|---|---|
| 被杀时的写入 | 直接写生产路径，**48 B** | 写暂存名 `.om-render-<pid>-<hex>.mp4`（48 B），生产文件 20,897,137 B |
| 工具返回 | success=false | success=false |
| **生产文件** | **20,897,137 B → 48 B**（SHA `64d52708…`） | **20,897,137 B，SHA `47d46d72…` 不变** |
| 目录残留 | — | 无（失败路径丢弃暂存） |
| 同路径重试（无故障，GPU 渲染） | — | success=true，`gpu_execution.backend=gpu`，`delivery={mode: staged, published: true}`，成片 411,252 B（SHA `f05ecaa7…`），目录只剩生产文件 |

验收方原始 `mux_cancel.py`（路径改写副本，`reviewer_harness/`）在修复后：其触发条件（生产路径出现在 argv 里）不再成立，渲染成功并发布（467,300 B 有效成片），注入未触发——见 §8 末段说明。

**同形的 compose 交付（`compose_fault.py`）**：对已有输出再次 `compose`，最终交付的 FFmpeg 在打开输出后死亡（注入）。

| | 修复前（HEAD 导出） | 修复后 |
|---|---|---|
| 工具返回 | success=false | success=false |
| 既有成片（1,426,763 B，SHA `63e930e9…`） | **→ 100 B**（`cd00e292…`） | **1,426,763 B，SHA `63e930e9…` 不变**，无暂存残留 |

### 3.4 F2：选项作用域（`f2.py`；源为 4:4:4 10-bit 的 H.264 + AAC，输出 mp4）

CPU 基线 = `/usr/bin/ffmpeg` 逐字运行原命令；调度器 = 真实 shim，`OM_GPU_MODE=auto`。

| 命令选项（位于 `-i` 与输出之间） | CPU 基线（视频） | 修复前调度器 | 修复后调度器 |
|---|---|---|---|
| `-c copy -c:v libx265 -pix_fmt yuv420p`（**验收方复现**） | **HEVC Main / yuv420p** | **H.264 High 4:4:4 Predictive / yuv444p10le**，遥测 `backend=gpu hevc_mediacodec rc=0` | **HEVC Main / yuv420p**，遥测 `gpu hevc_mediacodec`，`verified: hevc/yuv420p` |
| `-c copy -c:v libx264 -pix_fmt yuv420p` | H.264 High / yuv420p | 同（但硬件被 profile 拒绝 → 退回 CPU） | 同，**真实硬件编码** `h264_mediacodec`（`verified h264/yuv420p`） |
| `-c:v libx264 -pix_fmt yuv420p -c copy`（视频应为复制） | 复制：4:4:4 10-bit | 复制（遥测 `cpu` 回退） | 复制，**无任何 GPU 遥测声明** |
| `-c:v libx265 -pix_fmt yuv420p -c copy`（视频应为复制） | 复制：4:4:4 10-bit | 复制，但遥测**冒报** `gpu hevc_mediacodec` | 复制，**无 GPU 遥测声明** |
| `-vcodec libx264 -pix_fmt yuv420p -c:v copy` | 复制 | 复制 | 复制 |
| `-c copy -vcodec libx264 -pix_fmt yuv420p` | H.264 / yuv420p | 同（经 CPU 回退） | 同，硬件编码 |
| `-c:v libx264 -pix_fmt yuv420p -c:a aac` | H.264 / yuv420p | 同，硬件 | 同，硬件（`verified`） |
| `-c:v libx265 -pix_fmt yuv420p -c:a copy` | HEVC / yuv420p | 同，硬件 | 同，硬件（`verified`） |
| `-c:v:0 libx264 -pix_fmt yuv420p -c:a aac` | H.264 / yuv420p | 同，硬件 | 同，硬件 |
| `-c copy -c:0 libx264 -pix_fmt yuv420p`（流索引，无法静态判定） | H.264 / yuv420p | 同（走 CPU 直通，**遥测无记录**） | 同，**遥测 `cpu`：`stream-specific option -c:0 cannot be resolved statically`** |
| `-c:v libx264 -pix_fmt:v:0 yuv420p -c:a aac` | H.264 / yuv420p | 同（`cpu`：像素格式未识别） | 同，硬件 |

修复后 11/11 与 CPU 基线逐流一致（codec/profile/pix_fmt）；音频均为 AAC LC（复制或编码均与基线一致）。验收方原复现为 64×64：该尺寸硬件 HEVC 编码器返回 `Invalid argument`，修复后正确地回退 CPU 并产出 HEVC/yuv420p（遥测 `cpu`，原因含 `Invalid argument`）；大于 128×128 时走硬件（上表）。

### 3.5 其他真实硬件检查

* `-c libx264 -an -pix_fmt yuv420p`（通用 `-c` 赢家）→ 硬件 `h264_mediacodec`，`verified h264/yuv420p`。
* 两个输出、两种编码（`a.mp4` libx264 + aac，`b.mp4` libx265 + `-an`）→ 一条命令内两种硬件编码器：`encoder=h264_mediacodec,hevc_mediacodec`，`device=c2.mtk.avc.encoder,c2.mtk.hevc.encoder`，两个输出各自 `verified`，ffprobe 为 `h264` / `hevc`。
* **FFmpeg compose（真实硬件）**：两个 1080p 片段（取自 30 s 示例片）→ 两次 `h264_mediacodec` 硬件编码（`verified h264/yuv420p`，`c2.mtk.avc.encoder`），成片 1920×1080 h264/yuv420p + aac，3,954,595 B，SHA `bde9f4e2923e17c158c3877efbba7d8b0a565c92161f0404b3ec661ba8e060b3`；对同一路径**再次** compose（最终步骤走暂存+发布）得到**逐字节相同**的成片，目录无残留。
* **Remotion 真实 GPU 渲染**：3 次直接渲染（新路径）与 3 次对已有文件的暂存+发布渲染，全部 `backend=gpu`，1920×1080 h264，120 帧，**同一 SHA `9adaa7c261e6f1aabc60fb0a34432c4c7422aa32e23d4f51cfe6f920093a3ad7`（373,912 B）**——暂存发布不改变媒体字节。

---

## 4. 回归与测试

**新增回归测试（8 个文件，164 项：163 passed + 1 skipped（仅 root 下跳过的目录权限用例））**

| 文件 | 项数 | 覆盖 |
|---|---|---|
| `tests/lib/test_gpu_option_scoping.py` | 40 | 报告命令的就地改写；`-c:v … -c copy` 为复制；别名/说明符；无法判定的说明符保留 CPU；`-pass`；契约校验（错 codec、错像素格式、封面图、逐流）；**以真实 FFmpeg 为裁判的 20 种顺序组合**（计划与翻译后的有效 codec 必须与 FFmpeg 一致） |
| `tests/lib/test_gpu_output_ownership.py` | 51 | `file:` 解析（含相对、无百分号解码、host）；暂存/发布/符号链接/目录不可写；显式与隐式 HLS/DASH/chunk 等价；sidecar 选项；`tee:`/`md5:`/`crypto+file`；网络目标；别名与原地编辑；调度器拒绝（rc=78）；契约不符 → 不发布并改走 CPU/`require` 失败；遥测 `verified`/`software_outputs`/多设备 |
| `tests/lib/test_gpu_owned_passthrough.py` | 17（1 skip） | **真实 shim + 真实 FFmpeg**：覆盖已有文件的无加速命令成功原子发布、失败/取消(SIGTERM)/SIGKILL 均保留资产、写入者随调度器死亡、重试成功、多输出整体发布/整体不写、`file:` URL、符号链接、无 `-y` 与原地写同文件时保留 FFmpeg 自身拒绝、新路径不受影响、纯 HLS 不被拒 |
| `tests/lib/test_gpu_delivery.py` | 29 | `Delivery` 逐尝试暂存/发布/丢弃、权限位、符号链接、目录占位；`run_render`：GPU 成功/失败→CPU/**降级后 CPU 重渲失败仍保留旧成片**/require/超时/取消/孤儿晚到写入；`finalize` 步骤；新路径直写且命令不变；降级输出不留在新路径 |
| `tests/lib/test_gpu_oop_lost_connection.py` | 5 | 请求发出后断连=「可能已启动」→ 全新暂存名；无 worker 仍可复用 |
| `tests/lib/test_gpu_live_f1_f2.py` | 5 | **真实 MediaTek 硬件**：F2 报告命令得 HEVC/yuv420p 且遥测诚实；复制保持复制且不冒报；常规负载仍走硬件；`file:` URL 失败保留资产；隐式 HLS 被拒且旧分片 SHA 不变（无运行时则跳过） |
| `tests/tools/test_video_compose_delivery.py` | 9 | compose/burn/overlay/encode 的暂存交付、失败保留、原地写交给 FFmpeg、真实二次 compose；音频 mux 作为交付步骤、失败按「mux 失败」上报 |
| `tests/tools/test_hyperframes_delivery.py` | 8 | `render`/`render_existing`：覆盖已有文件的暂存发布、失败保留、新路径直写、rc=0 无产物为失败 |

**负控制（证明这些测试是真回归测试）**：
* 对旧 `ffmpeg_args`，真实 FFmpeg 裁判显示 **12/20** 个顺序组合的计划或翻译错误；新实现 20/20。
* 把 `test_gpu_owned_passthrough.py` 指向修复前的 `734c455` 调度器：**6 项失败**（失败后资产被截断为 0 B：`failure_after_output_opened`、`cancellation_mid_write`、`retry_after_a_failed_run`、`failed_multi_output`、`file_url_destination`、`cpu_only_reencode`），其余 10 项（须保持不变的行为）在前后均通过。

**既有测试（逐字未改）**
* 4 个 GPU 模块：`test_gpu_ffmpeg_args`、`test_gpu_dispatch_safety`、`test_gpu_oop`、`test_gpu_remotion_runner` → **127 passed**（与修复前相同）。
* 与本次改动相关的已批准工具测试（含对「渲染命令里出现最终输出路径」的断言与写原路径的假运行器）全部通过：`test_remotion_caption_public_path`、`test_cinematic_remotion_adapter`、`test_remotion_diagnostics`、`test_remotion_staging_lifecycle`、`test_remotion_audio_mux`、`test_video_compose_vertical`、`test_hyperframes_compose`、`tests/contracts/`、`tests/qa/test_05_video_compose.py`。
* **全量**：`.venv/bin/python -m pytest tests/ -q` → **2280 passed, 12 skipped, 3 xfailed, 1 subtests passed**（EXIT=0，22 分 43 秒；前值 2117 passed, 11 skipped, 3 xfailed，增量 163 passed + 1 skipped 即本轮新增测试）。

测试过程中发现并已修正的一个**测试自身**缺陷：全量运行时 `import tools` 会把调度器 shim 置于 PATH 首位，新测试若以 `shutil.which("ffmpeg")` 取"真实 FFmpeg"会拿到 shim，使 shim 自我 exec 死循环；现统一用「排除 shim 目录」的解析函数。产品代码无此问题（`real_ffmpeg()` 本来就跳过 shim 目录）。

---

## 5. 性能

同一台共享设备上**交替**运行（先后顺序交错以抵消温度与他人负载），每项 12 次取中位数；「旧」=`734c455` 的 `lib/` 与 shim 导出，「新」=工作区。

| 场景 | 旧（中位） | 新（中位） | 差 |
|---|---|---|---|
| stream copy → **新**文件（零开销 exec 快路径） | 0.668 s | 0.653 s | −2%（噪声内；未新增导入） |
| stream copy → **已有**文件（旧：直写；新：被拥有的子进程 + 原子发布） | 0.633 s | 0.757 s | **+0.12 s**（Python 监管层，仅在会覆盖已有文件时） |
| 硬件编码 720p 6 s → 新文件 | 4.775 s | 4.642 s | −3% |
| 硬件编码 720p 6 s → 已有文件（旧版已暂存） | 4.770 s | 4.707 s | −1% |
| CPU 重编码（无 `-pix_fmt`）→ 已有文件 | 1.187 s | 1.241 s | +0.05 s（+5%） |
| Remotion 1080p/3 s 真实 GPU 渲染：直写新路径 vs 暂存+发布已有路径（各 3 次） | 60.5 s | 60.7 s | +0.3%（噪声内），**输出 SHA 完全相同** |

结论：GPU 硬件编码与 Remotion GPU 渲染无可测回退；唯一的系统性开销是「覆盖已有文件的无加速命令」多一层约 0.1 s 的进程监管（Python 导入在 PRoot 下较慢，已把导入精简到 `subprocess`+`signal`），相对任何实际编码/渲染可忽略。校验使用的 `ffprobe` 与旧版的 `v:0` 校验同量级（现取全部视频流，一次调用）。设备为共享机，±5% 的差异在重复测量间属于噪声。

---

## 6. 修改文件

| 文件 | 变更 |
|---|---|
| `lib/gpu/ffmpeg_args.py` | URL/路径解析（`url_scheme`/`local_path`/`write_target`）、有效 muxer（`effective_muxer`，含隐式 HLS/DASH/chunk）、sidecar 选项与写文件协议、选项作用域模型（`_canon`/`_scoped_last`/`_single_video_stream`）、就地改写翻译、`OutputPlan.codec_index/expect`、`OutputOwnership.path`、目的地别名/原地编辑检测 |
| `lib/gpu/ffmpeg_exec.py` | 按令牌/真实路径暂存（`file:`、符号链接、目录不可写）、`_run_cpu_owned`/`_passthrough`（覆盖既有文件的无加速命令）、`_validate` 的编码/像素格式契约与 `_VERIFIED`、遥测（`verified`/`software_outputs`/多编码器与设备）、`_load_proc` 精简导入、取消处理器可重入 |
| `lib/gpu/delivery.py`（新） | `Delivery`：覆盖已存在文件时的逐尝试暂存、校验、原子发布与丢弃；`retarget` |
| `lib/gpu/remotion.py` | `run_render(output=, finalize=)`：渲染边界所有权，GPU→CPU 重渲各用独立目的地 |
| `lib/gpu/oop.py` | 请求发出后断连报告为「可能已启动」（`_lost`），触发全新暂存名 |
| `lib/gpu/telemetry.py` | `summarize` 带出 `verified`/`software_outputs` |
| `tools/video/video_compose.py` | `_run_ffmpeg_to`（compose 最终步骤与 burn/overlay/encode）、Remotion 两条路径传入 `output=`、音频 mux 作为 `finalize` 步骤（`_audio_finalizer`/`_PostRenderFailure`） |
| `tools/video/remotion_caption_burn.py` | 传入 `output=` |
| `tools/video/hyperframes_compose.py` | `render`/`render_existing` 的交付暂存 |
| `gpu_native/README.md`、`gpu_native/REPORT.zh-CN.md` | 策略与所有权说明、报告指针 |
| 新增测试（8 个文件） | 见 §4 |

**未改动**：既有的 4 个 GPU 测试模块与所有已批准测试（逐字未改）、`gpu_native/` 下的原生运行时/worker/脚本、Android/PRoot/驱动。

---

## 7. 剩余限制

1. **SIGKILL 的残留**：被 `SIGKILL`（含 Android OOM 杀进程）的调度器无法运行清理，会留下一个**隐藏的暂存文件**（`.om-gpu-*` / `.om-render-*`，其写入者随调度器的 `PR_SET_PDEATHSIG` 一并死亡，不会再写）；生产文件保持原样，该残留文件可安全删除。未引入清扫器。
2. **不可接管的输出照旧透传**：未被加速的纯 HLS/segment/编号图像序列命令（例如上传/抽帧管线）保持原样直通，不被拒绝；只有「被加速的命令同时含有不可接管输出」才 `rc=78`。HLS/DASH 的分片集合没有被整体事务化（需求为「无法接管则拒绝」，不要求造这种事务）。
3. **只保护「覆盖已存在文件」的情形**：写**新**路径的命令失败/取消后仍可能留下残缺的新文件（与之前一致，没有既有资产被毁）；硬链接输出会在其路径上被新文件替换（其它硬链接仍指向旧内容）；目录不可写但文件可写的情形回到原地写。
4. **取消语义对「实时采集」的影响**：覆盖已存在文件的实时采集若用 SIGINT/SIGTERM 结束，将不发布而保留旧文件；用 `q`、`-t` 或自然结束则正常发布。仓库内置采集工具均使用 `-t`。
5. **`OM_GPU_MODE=cpu`（或原生运行时未安装）时** PATH 上没有调度器，FFmpeg 直通层面的所有权不适用（与上游基线一致）；渲染/合成/HyperFrames 的交付边界（`lib/gpu/delivery.py`）不依赖 GPU 模式，仍然生效。
6. **范围之外**：`Upscale` 的静态图最终 `cv2.imwrite`、HyperFrames CLI 自身的非 FFmpeg 写盘未做改动；`Delivery` 不保证「Python 工具进程被 SIGKILL 后」孤儿 Remotion 进程不再写其**自己的**暂存文件（它们写不到生产路径）。
7. 硬件 HEVC 编码器拒绝 64×64 这类过小分辨率（`Invalid argument`）：对该尺寸，命令正确地回退到 CPU 并产出 HEVC/yuv420p；验收方原复现之所以「硬件成功」，恰是因为硬件编码器根本没有被执行（被 `-c copy` 覆盖）。
8. **本报告不构成独立 L5 通过结论。**

---

## 8. 复现步骤

```bash
# 证据脚本（私有 OM_GPU_HOME；运行时二进制只读链接）。前=734c455，后=工作区。
cd /tmp/om-f1f2-remediation/evidence
python f1a.py after      # file: URL：混合失败/单失败/成功覆盖/取消(SIGTERM,SIGKILL)/相对 file:/失败后重试
python f1b.py after      # 隐式 HLS/DASH/sidecar + 被阻塞的第二输出；纯 HLS 仍可用
python f2.py  after      # 11 种 -c / -c:v / -vcodec / -c:v:0 / -c:0 顺序，CPU 原命令 vs 调度器，ffprobe + 遥测
python f1c.py after both # Remotion 真实 GPU 渲染到已有文件：最终 mux 中途 SIGKILL，随后同路径重试
python compose_live.py after   # 真实硬件上的 FFmpeg compose，及对已有输出的二次 compose
python compose_fault.py after /root/projects/openmontage   # compose 最终步骤在打开输出后死亡（注入）
#   修复前：先 `git archive 734c455 | tar -x -C /tmp/om-f1f2-remediation/headfull`，再
#   python compose_fault.py before /tmp/om-f1f2-remediation/headfull
python perf.py 12; python remotion_perf.py 3   # perf.py 用到 ../headrepo（734c455 的 lib 与 shim 导出）
python collect.py              # 把 before/after 结果打印成表

# 回归
.venv/bin/python -m pytest tests/lib/test_gpu_option_scoping.py tests/lib/test_gpu_output_ownership.py \
    tests/lib/test_gpu_owned_passthrough.py tests/lib/test_gpu_delivery.py tests/lib/test_gpu_oop_lost_connection.py \
    tests/lib/test_gpu_live_f1_f2.py tests/tools/test_video_compose_delivery.py tests/tools/test_hyperframes_delivery.py -q
.venv/bin/python -m pytest tests/lib/test_gpu_ffmpeg_args.py tests/lib/test_gpu_dispatch_safety.py \
    tests/lib/test_gpu_oop.py tests/lib/test_gpu_remotion_runner.py -q
.venv/bin/python -m pytest tests/ -q
```

**关于验收方 `mux_cancel.py` 的触发条件。** 该注入器仅在最终 mux 的 argv 中**出现生产路径**时才介入（`str(DEST) in args`）。修复后，覆盖已有成片的渲染把最终 mux 指向**独立暂存路径**，生产路径不再出现在该命令里，因此该注入器的触发条件不再成立（渲染正常成功并原子发布）；本报告的 `fault_ffmpeg.py` 改为盯住目标目录里的写入（生产文件尺寸变化或同目录新出现的暂存文件）后 SIGKILL 该 FFmpeg 进程组，故前/后使用同一把「中途杀写入者」的刀。验收方复验时请据此调整触发条件。
