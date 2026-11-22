# CFSCAN-SNI

本地运行的 Python Cloudflare 优选 IP 工具。沿用 cfnb 的“本地配置 + 多候选源 + TCP 初筛 + 验证 + 结果文件 + 自动同步”结构，采用 CFIP 的先验证、后测速思路，提供 Lite 地区校准、两种地区口径、可选 Zone 缓存、反代出口验证及过滤、下载测速，以及可确认的 GitHub 推送状态。当前版本 1.3.0。

```text
读取配置并加运行锁 → 聚合候选文件/URL 并按 IP:端口去重 → 按端口过滤
    ↓
IPinfo Lite 校准并缓存（两种地区口径都经过此阶段）
    ↓
开启分地区：按 Lite 国家筛选；关闭分地区：保留全部国家
    ↓
对剩余全部候选做 TCP 测试 → 按 TCP 延迟升序，全局或各地区取前 N 个（0 不限）
    ↓
入选者进入 Zone 缓存 / 实际 trace 验证
    ├─ 关闭反代验证：直接进入后续流程，出口类型和风险过滤均跳过
    └─ 开启反代验证：检查出口变化 → 按设置过滤出口类型 → 可选出口风险过滤
    ↓
机房国家口径在 Zone 后筛选/分组；IP 国家口径沿用 Lite 国家
    ↓
开启测速：按延迟选测速名单 → 缓存节点复验 → 按设置的 IP 并发、单 IP 连接数和轮数下载
关闭测速：按 Zone 延迟选结果 → 缓存节点在输出前复验
    ↓
分地区配额 / 全局 Top N → 原子替换 ip.txt → 可选 GitHub 提交、推送并读取远端 SHA 确认
```

## 快速开始

需要 Python 3.10+。Git 只在启用 GitHub 同步时需要。

Windows PowerShell：

```powershell
cd C:\path\to\CFSCAN-SNI
.\setup.ps1
notepad config.json
notepad ipinfo_tokens.txt
.\start.cmd check
.\start.cmd --no-github
```

如果 PowerShell 脚本受限，可手动安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py init
```

Linux / macOS：

```bash
bash setup.sh
# 编辑 config.json，填写自己的域名；编辑 ipinfo_tokens.txt，填入 Lite Token
.venv/bin/python main.py check
.venv/bin/python main.py --no-github
```

`setup` 不覆盖已有配置。所有相对文件路径都以 `--config` 指定的配置文件所在目录为基准。`check` 只校验配置结构，不代表网络或 Zone 已通过验证。

运行时请使用上面的 `.venv` 内 Python；直接使用系统 `python3 main.py` 可能找不到已装在虚拟环境中的 Babel 等依赖。Linux 使用 `bash setup.sh` 即可安装，不要求脚本具有可执行权限。

## 完整处理流程

一次正常优选按下面的顺序执行。后文各配置章节可用于调整对应阶段。

1. **读取配置并加运行锁。** 校验域名、端口、地区配额、缓存路径和测速参数，阻止同一配置目录下的多个任务同时修改结果。
2. **聚合候选并去重。** 从 `sources` 读取文件或 URL，解析为 `IP:端口`；没写端口时使用 `default_port`。同一 IP 的不同端口分别验证和测速。CIDR 按 `cidr_samples` 固定抽样，默认每个网段最多取 64 个地址。`source_max_candidates` 在此阶段检查，发生在端口过滤和 TCP 前 N 筛选之前。
3. **过滤端口。** 只保留 `candidates.ports` 允许的候选，`[]` 表示不限端口。全量和抽样模式都执行此步骤，并且只筛选已有端口，不改写端口。
4. **Lite 地区校准。** 对端口过滤后的全部候选，先查 Lite 缓存，只有缺失或过期的唯一 IP 才查询 API。默认跳过校准失败的 IP，`require_all: true` 时存在校准失败就终止本轮。`colo` 模式也执行此步骤。
5. **可选地区筛选。** `regional_mode: true` 且 `region_basis: "ipinfo"` 时，按 `regions` 筛选目标国家；`regional_mode: false` 不按 `regions` 过滤国家，最终全局取 `top_n`。机房国家需从 Zone 响应确定，因此 `colo` 模式的国家筛选放到 Zone 后，TCP 前 N 筛选只支持 `global`。
6. **全部剩余候选 TCP 测试，再按延迟取前 N 个。** 端口、校准和地区筛选后的全部候选逐一纳入测试，按 `tcp.concurrency` 并发。每个候选按 `tcp.rounds` 依次建立新 TCP 连接，达到 `tcp.min_success_rate` 且至少成功一次才通过。延迟取成功样本的最大值，另记标准差。随后严格按 TCP 延迟从低到高排序，以 `candidates.sample_size` 取前 N 个；`global` 全局取 N 个，`per_region` 每个 Lite 国家各取 N 个，`0` 全部保留。成功次数仅用于成功率筛选，不覆盖延迟排序；同延迟按 IP:端口确定固定次序。仅入选者进入 Zone，名单外候选本轮不补测。TCP 不发 HTTP、不调用 Worker，每轮重新测试。
7. **Zone 缓存或实际验证。** `zone.cache_enabled: false` 时不读写缓存，逐个实际验证；开启时有效的失败缓存直接跳过，有效的通过缓存进入下一阶段。无缓存或过期则连接候选 IP 和端口，以 `zone.domain` 作为 SNI/Host 请求 `zone.path`，检查 TLS、状态、Cloudflare 响应头、trace 字段及可选标记、机房限制。默认两轮，必须全部通过。`colo` 模式随后按实测机房筛选/分组。
8. **可选反代出口过滤。** 只有 `proxyip.enabled: true` 才在第 7 步先获取直连基准并检查每轮出口变化，验证通过后按 `exit_types` 过滤，再按 `risk.enabled` 决定是否查询实际出口风险。关闭总开关时三项均跳过。风险按唯一出口缓存，缓存节点复验后重新过滤新出口。
9. **按测速开关处理。** 开启 `speed.enabled` 时，每组按 Zone 延迟选择最多 `max(speed.candidates_per_region, 输出配额)` 个测速候选，设为 `0` 则测全部。缓存通过的入选节点先实时复验，再按 `speed.concurrency` 并发测试 IP、按 `streams` 并发下载、按 `repeats` 依次重复；各轮必须有效，再取速度中位数、检查最低速度。关闭测速时完全跳过下载和测速口令读取，忽略最低速度及测速名单数量，按 Zone 延迟直接选取；缓存节点输出前仍实时复验，失败时继续检查后面的候选填补配额。`speed.max_latency_ms` 作为 Zone 延迟上限，在两种模式下均生效。
10. **排序、取配额、写结果。** 开启测速时按 `speed.sort_by` 的速度/延迟优先规则排序，关闭时固定按 Zone 延迟。分地区模式按 `regions` 配额，关闭分地区则全局取 `top_n`。测速名单内失败不会从名单外补测；缺额输出实际数量。整轮无结果保留旧 `ip.txt`，有结果原子替换。关闭测速时不输出伪造的 `0Mbps`，报告通过 `speed_enabled: false` 标明未测速。
11. **按需同步 GitHub。** 启用 `github.enabled` 且本次未指定 `--no-github` 时，提交已写入的输出文件、推送，并单独读取实际 push URL 的远端分支 SHA 确认结果。推送失败保留本地结果并返回退出码 2。

### 三种数量分别控制什么

| 参数 | 控制阶段 | 示例含义 |
| --- | --- | --- |
| `candidates.sample_size` | TCP 通过后进入 Zone 的数量上限 | `1000`：按 TCP 延迟升序全局取前 1000 条，或每个 Lite 国家各取前 1000 条，由 `sample_scope` 决定；`0` 不限数量 |
| `speed.candidates_per_region` | 每个地区纳入下载测速名单的数量 | `12`：优先测延迟最低的 12 条；输出配额大于 12 时取配额数量，实际候选不足则取现有数量；`0` 表示测全部通过候选 |
| `regions` / `top_n` | 最终输出数量 | 分地区时 `{"HK": 10, "JP": 10}` 各最多 10 条；全局时用 `top_n` |

例如，某地区 Zone 通过 500 条，`candidates_per_region: 12`、输出配额为 10 时，先取延迟最低的 12 条作为测速名单，再从测速成功者中选最多 10 条。**最终结果来自这 12 条的比较，不代表已经测完其余 488 条。** 如果其中只有 8 条通过，就输出 8 条并报告缺额。

## 必填配置与按地区运行

`config.example.json` 使用标准 JSON，并通过 `_comment` / `_comment_参数名` 字段直接说明用途、默认值和 cfnb 参考值；这些说明字段不参与运行，可以保留或删除。`init` / 安装脚本创建的配置也会包含说明，已有配置不自动覆盖。

`regional_mode: true` 是默认值，按 `regions` 筛选并分别取配额。关闭地区筛选、所有国家共同竞争 15 个名额：

```json
{
  "regional_mode": false,
  "top_n": 15
}
```

全局模式仍保留每个结果的真实国家代码和中文名称。`regions` 此时不限制候选也不控制输出数量。`region_basis` 仍决定标签使用 Lite 国家还是实测机房国家。

复制的 `config.json` 中先填写自己的 Cloudflare 域名：

```json
{
  "zone": {"domain": "cdn.your-domain.com"},
  "regions": {"HK": 3, "JP": 3, "SG": 3, "US": 3},
  "region_basis": "ipinfo"
}
```

这里只展示需要改的字段；其他字段参考完整的 `config.example.json`。

- `regions`：每个国家/地区独立的输出配额。`HK:3,JP:5` 表示香港最多 3 个、日本最多 5 个。
- `region_basis: "ipinfo"`：按 Lite 校准得到的 **IP 所属国家** 过滤，然后验证、测速。适合基于 IP 归属的列表。
- `region_basis: "colo"`：仍先执行 Lite 校准，再在 Zone 验证后读取 `CF-Ray` 后缀，例如 `SIN`，按 **实测边缘机房国家** 分组。适合关注当前网络实际接入地区的场景。
- `colo_countries`：内置常用机房到国家代码映射，也可在配置里补充。未收录的机房明确写入拒绝报告，不能确认时不猜国家。
- `zone.allowed_colos`：可进一步只保留特定机房，例如 `["HKG", "NRT", "KIX", "SIN"]`。
- `regions: {"*": 2}`：每个已发现的国家最多输出 2 个。

临时覆盖，不修改配置文件：

```powershell
.\start.cmd --regions HK:3,JP:5,SG:3 --region-basis colo --no-github
```

**IP 所属国家与接入机房国家是不同口径。** `report.json` 同时保留 Lite 国家、实测 `colo` 和最终分组国家。程序不能通过地区选择强制改变网络路由，候选不足时会报告缺额，不把其他国家塞进来补齐。

## 候选来源

`sources` 可以同时配置本地文件、公开文本 URL 和 JSON URL：

```json
{
  "sources": [
    "candidates.txt",
    {"url": "https://your-source.example/all.txt", "enabled": true}
  ]
}
```

支持 IPv4、IPv6、带端口、`#` 注释、常见 JSON 的 `ip/address/server + port` 以及 CIDR。示例中的文档地址不是可用节点：

```text
192.0.2.1:443#原标签
[2001:db8::1]:8443#原标签
192.0.2.0/24
```

源文件原标签不作为最终国家依据；端口过滤后先做 Lite 校准，再按设置筛选地区、全部 TCP 测试、按 TCP 延迟选前 N 个。IP+端口去重，同一个 IP 的地区信息只查询一次。

默认不限制 TCP 通过候选进入 Zone 的数量，可按下一节设置 TCP 延迟前 N 个。CIDR 本身没有地区信息，程序通过 `cidr_samples` 对每个网段做固定、分层抽样，IPv6 大网段也不会展开成巨量地址。CIDR 展开与 TCP 后选前 N 个是两个独立步骤。`source_max_candidates` 是聚合候选时的总量保护，在端口过滤和 TCP 筛选之前检查，超过就报错；需要接收更大的源时应相应提高此上限。

## 端口过滤与 TCP 延迟前 N 筛选

在 `config.json` 顶层加入 `candidates`。例如，只保留 443 端口候选，完成 Lite 校准和可选地区筛选，对剩余全部候选做 TCP 测试后，取通过者中 TCP 延迟最低的前 1000 条进入 Zone 和可选测速：

```json
{
  "candidates": {
    "ports": [443],
    "sample_size": 1000,
    "sample_scope": "global"
  }
}
```

- `ports`：允许的端口列表，例如 `[443]` 或 `[443, 8443, 2053]`；默认 `[]` 表示不过滤。端口过滤在 IPinfo 校准前完成，无论全量还是抽样模式都会生效。它只筛选候选，不会把源里的 8443 改写成 443；没有显式端口的地址先使用 `default_port`。
- `sample_size`：保留旧字段名，现表示 TCP 通过者按延迟升序保留前 N 个，不再随机。必须为非负整数，默认 `0` 表示全部通过者进入 Zone。每条 `IP:端口` 算一个候选，同 IP 不同端口分别计数。不足 N 取现有数量，不重复、不扩大预算；Zone 失败不从名单外补测。
- `sample_scope: "global"`：所有 TCP 通过候选共同排序，取延迟最低前 `sample_size` 条。减少 Zone 及后续下载请求，不减少 Lite 和 TCP 测试量；各地区的入选数量取决于实际 TCP 延迟。
- `sample_scope: "per_region"`：每个 Lite 国家在 TCP 通过者中分别按延迟排序，取前 `sample_size` 条进入 Zone。适用于 `region_basis: "ipinfo"`，避免某一地区占满全部候选名额。关闭分地区模式时也可这样分配候选预算，最终仍只用全局 `top_n`。

全量只测 443 时使用 `"ports": [443], "sample_size": 0`。每个国家各取 TCP 延迟最低的前 100 条，使用 `"sample_size": 100, "sample_scope": "per_region"`。例如日本 TCP 通过 800 条、新加坡通过 500 条，此设置分别保留两国最低延迟的 100 条，共 200 条进入 Zone。这里的数量与 `regions` 的最终输出数量是两回事。

`region_basis: "colo"` 需要 Zone 响应才能知道机房国家，因此限制 TCP 前 N 数量时只支持 `global`，不能根据 IPinfo 国家冒充机房国家提前分组。TCP 延迟低不保证下载速度最快；此筛选只限定后续验证和下载的候选范围。日志明确显示“TCP 延迟前 N 筛选”；报告的 `candidate_pool` 记录 `before_tcp`（地区筛选后全部候选）、`after_tcp`（TCP 通过）、`after_tcp_selection` / `before_zone`（取前 N 后），`selection_method: "tcp_latency"` 标明选取方式，保留 `sampled_regions` 字段记录各地区通过/入选数量。

## 并发、连接数、轮数与 cfnb 参考值

下表的 cfnb 值按 2026-11-22 的原仓库配置/代码核对。参考值用于理解参数，不能使两个实现的测量口径完全相同。

| CFSCAN-SNI 参数 | 本项目默认 | 用途 | cfnb 参考及本项目设置建议 |
| --- | --- | --- | --- |
| `tcp.concurrency` | 300 | 同时测多少个 IP:端口 | `MAX_WORKERS=300`；本机负载高可降为 50～100 |
| `tcp.rounds` | 1 | 每候选依次重建 TCP 的次数 | `TCP_PROBES=1`；检查稳定性可设 3 |
| `tcp.timeout_seconds` | 3 | 单次 TCP 超时秒数 | `TIMEOUT=3` |
| `tcp.min_success_rate` | 1.0 | TCP 成功次数 / 轮数最低值 | 原版也默认 1.0；即全部成功 |
| `ipinfo.concurrency` | 8 | 同时校准多少个唯一 IP | 原版配置 300；本项目建议先用 8，仍受 API 限速和最小间隔影响 |
| `ipinfo.retries` | 2 | API 失败后的额外重试次数 | 不是测试轮数或并发数 |
| `zone.concurrency` | 48 | 同时验证多少个候选 | 原版 300，其配置建议延迟/抖动排序用 32～50；这里建议保留 48 |
| `zone.rounds` | 2 | 每候选依次验证几轮 | 原版至少 3；节省请求可保留 2，观察抖动可设 3 |
| `zone.connect_timeout_seconds` | 2 | 连接/TLS 建连超时秒数 | 原版 3；需要更宽松可设 3 |
| `zone.timeout_seconds` | 5 | 单轮完整请求总超时秒数 | 原版读取超时 3、总时长不设限，不能直接等同 |
| `speed.concurrency` | 1 | 同时下载测速多少个候选 | 原版 `BANDWIDTH_WORKERS=3`；设 3 接近其 IP 并发，设 1 减少互抢带宽 |
| `speed.streams` | 4 | 同一候选每轮同时开几路下载 | 原版每个候选一个 curl 请求，可设 1 接近其单请求测法 |
| `speed.repeats` | 3 | 同一候选依次测几轮 | 原版通常一轮 curl；可设 1。原版结果全空时的整体重试不等于每 IP 固定 3 轮 |
| `speed.connect_timeout_seconds` | 3 | 下载建连超时秒数 | 原版 curl 默认 3 |
| `speed.timeout_seconds` | 8 | 下载读取停滞超时秒数 | 原版 curl 总超时 3，口径不同；总测速预算用 `duration_seconds` |

例如 `speed.concurrency: 3, streams: 4, repeats: 3` 表示同时测最多 3 个候选，每个最多 4 路下载，这一批最多约 12 路下载连接；每个候选的 3 轮依次执行，不会同时开 36 路。TCP、Zone、测速分阶段执行，其并发数不相加。

若只想接近 cfnb 的请求规模，可设 TCP 并发 300、1 轮、3 秒；Zone 并发 48、3 轮、连接 3 秒；测速 IP 并发 3、单 IP 1 路、重复 1 轮。仍保留本项目的总时长/字节预算、证书和下载正文校验，不等同于直接运行 cfnb。

### 原版延迟、抖动和综合排序权重

cfnb 的 TCP 延迟取成功连接样本的最大值；HTTP 延迟取多轮探测耗时的最大值；HTTP 抖动是这些耗时的总体标准差。虽然其配置把 HTTP 指标称作首字节延迟，当前代码在完整读取 trace 正文后才结束计时。HTTP 层还包含 TLS/请求/响应过程，不能和纯 TCP 延迟混为一谈。标准差衡量所测样本的波动，不等于丢包率，也不保证未来稳定。

原版先按 TCP 成功次数多、TCP 延迟低选出有限验证池；有测速结果时再算综合分，按分数降序选全局或各国 Top N：

```text
score = SPEED_WEIGHT × Mbps
        / (1 + TCP_LATENCY_WEIGHT × TCP秒数
             + HTTP_LATENCY_WEIGHT × HTTP秒数
             + JITTER_WEIGHT × HTTP抖动秒数)
```

延迟和抖动权重越大，相关指标高的节点扣分越重。例如 TCP 权重 0，HTTP/抖动权重都为 3、速度权重 1：A 为 80Mbps / 80ms / 抖动10ms，得分约 63；B 为 100Mbps / 400ms / 抖动150ms，得分约 38。此时较慢但响应稳定的 A 排在 B 前面。权重不是百分比，不需要相加等于 1 或 100。

`SPEED_WEIGHT` 在该公式里统一乘在所有节点分子上，正数从 3 改 6 只会把全部分数翻倍，不改变名次；设为 0 则让分数全为 0。需要改变速度和响应质量之间的取舍，应调分母中的惩罚权重，而不能只调速度常数。原版默认 TCP 权重 0、`HTTP_LATENCY_ENABLED: false`，正常有测速结果时最终基本按速度排序。其源码在关闭 HTTP 开关时采用相同的大缺省延迟/抖动值，分母成为常数，并非继续比较真实 HTTP 指标。

本项目保留直接排序：`speed.sort_by: "speed"` 为速度降序、同速延迟升序；`"latency"` 为 Zone 延迟升序、同延迟速度降序；关闭测速固定延迟升序。Zone 延迟用收到响应头耗时的中位数，抖动用总体标准差。TCP 指标用于连接筛选、安排 Zone 验证顺序和报告；抖动目前仅写报告，不参与最终排序；当前未引入综合权重开关。输出没有通过验证/测速时保留旧结果，避免原版某些失败回退分支绕过预期筛选。

## IPinfo Lite

使用 Lite 路径 `https://api.ipinfo.io/lite/{ip}?token=...`，解析 **`country_code`**；Lite 的 `country` 是英文全名，不能沿用旧接口把它当国家代码。

Token 只从本地 `ipinfo_tokens.txt` 读取，一行一个。文件名可通过 `ipinfo.token_file` 修改，相对路径基于配置文件所在目录：

```json
{
  "ipinfo": {
    "token_file": "ipinfo_tokens.txt"
  }
}
```

文件支持 UTF-8 BOM、空行和以 `#` 开头的注释，重复 Token 会去重。不要保留示例占位值。本地工具不再读取任何 Token 环境变量。

有多 Token 时轮换查询，401/403 停用对应 Token；429 和临时网络错误做有限重试。成功结果默认缓存在 `.state/ipinfo_lite.json`，有效期由 `ipinfo.cache_ttl_hours` 控制，默认 168 小时（7 天）；按唯一 IP 复用，不按端口重复查询。缺失或过期的结果会重新查询，查询失败的 IP 默认跳过；`ipinfo.require_all=true` 时只要存在校准失败就终止本次输出。若本次所需 IP 全部命中有效缓存，则不会发起 Lite 查询。

`ipinfo.trust_env` 控制 IPinfo 请求是否采用环境中的代理等网络设置，与 Token 来源无关；Zone 验证和下载测速不读取系统 HTTP 代理，以便固定连接候选 IP。

国家名使用 Babel 中文地区字典，并统一输出 `中国香港 / 中国澳门 / 中国台湾`。`config.json`、Token、缓存和运行状态默认不进入 Git。

## Zone 验证

连接目标是候选 IP，TLS SNI 和 HTTP Host 是 `zone.domain`。默认开启证书校验，HTTPS 端口采用候选自己的端口，测试请求不读取系统 HTTP 代理。

从 1.1.2 起，新的配置默认使用 `zone.path: "/cdn-cgi/trace"`。该路径由 Cloudflare 边缘服务响应，批量初筛无需访问 Worker 首页。安装脚本不会覆盖已有 `config.json`；已有配置明确写了 `"path": "/"` 时，升级后仍会请求首页，需要手动修改现有 `zone` 中的以下字段，其他设置保留：

```json
"path": "/cdn-cgi/trace",
"expected_body": ""
```

请求路径为 `/cdn-cgi/trace`（可带查询参数）时自动启用 trace 字段校验，不需要增加开关：

| 字段或条件 | 通过要求 |
| --- | --- |
| HTTP 状态、响应头 | 必须为 200，并包含 Cloudflare Server 与 CF-Ray 响应头；即使放宽通用页面的状态码或响应头配置，也不会跳过 trace 的这些检查 |
| `h` | 与 `zone.domain` 匹配，忽略域名大小写和末尾点；可以不带端口，带端口时必须是本次候选端口 |
| `uag` | 与本次发送的 User-Agent 完全一致 |
| `ip` | 必须能解析为 IPv4 或 IPv6；这是 Cloudflare 看到的请求来源地址，不要求等于候选 IP；默认入口模式不要求与本机公网 IP 不同，开启反代模式才检查同族出口变化 |
| `visit_scheme`、`tls` | 协议与本次 HTTP/HTTPS 请求一致；HTTPS 要求有效 TLS 版本，HTTP 要求 `tls=off` |
| `colo` | 必须是有效机房代码，并与本次 CF-Ray 后缀一致，再执行 `allowed_colos` 筛选 |

缺少必要字段、重复字段、格式异常、错误页和超过 64 KiB 的 trace 正文都会拒绝。未知的新字段允许保留，必需字段仍逐项校验。不保存完整 trace 正文；默认普通入口模式不保存 `ip=`，也不要求出口变化。只有开启 `proxyip.enabled` 才提取并保存已校验的出口 IP，用于反代验证、类型筛选和可选风险过滤。

自定义其他路径（例如 `/health` 或旧配置的 `/`）时，仍使用通用页面校验：

- 验证正文里的 Cloudflare 错误，例如 1034、1033、1016、1020，同时处理纯文本、HTML 标签和常见 JSON 形式。
- 验证可接受 HTTP 状态及 Cloudflare 响应头；普通 403 默认也不会通过。
- `zone.expected_body` 可设置自己页面的特征文本，进一步确认确实访问到了自己的服务。只检查响应正文前 64 KiB；trace 路径也会额外执行此项，因此切换路径时应清除首页专用的标记。
- 不自动跟随验证重定向，避免跳到另一个 IP 后误判；跳转站可把 `zone.path` 改成明确的探测路径。
- 全部验证失败不会回退为“全部有效”。

默认 `zone.rounds: 2`、`zone.concurrency: 48`：单个候选需要完成两轮验证，最多并行处理 48 个候选。**Zone 通过只说明验证域名和路径满足规则，测速 URL 仍需返回合格的下载正文。** 修改 `zone.domain` 不会自动修改 `speed.url`。

trace 通过不代表 Worker 代码、WebSocket/gRPC/XHTTP 隧道或目标网站转发已经通过测试。使用同域名 Worker 的 `/__speed` 测速时，后续下载仍会进入 Worker 并产生请求用量。改用 trace 只减少批量 Zone 阶段对 Worker 的调用。

### Zone 结果缓存

Zone 验证结果默认保存到配置目录下的 `.state/zone_cache.json`。可在现有 `zone` 对象内设置：

```json
{
  "zone": {
    "cache_enabled": true,
    "cache_file": ".state/zone_cache.json",
    "cache_ttl_hours": 6,
    "cache_failure_ttl_hours": 0.25
  }
}
```

- `cache_enabled`：缓存总开关，默认 `true`；设为 `false` 不读取、不更新缓存文件，每轮实际验证。重新开启后，未过期且规则匹配的旧条目仍可使用，不会重新计时。
- `cache_file`：缓存文件路径，相对路径以配置文件所在目录为准。
- `cache_ttl_hours`：通过结果的有效期，默认 6 小时；设为 `0` 关闭整个 Zone 缓存，恢复每轮实际验证。
- `cache_failure_ttl_hours`：失败结果的有效期，默认 0.25 小时（15 分钟）；设为 `0` 则不缓存失败，仍可缓存通过结果。临时连接故障不会沿用与通过结果同样长的有效期。

缓存以 **IP + 端口 + Zone 校验规则 + 反代模式/直连基准指纹** 区分，包含通过/失败原因、机房和延迟信息。修改域名、路径、HTTP/HTTPS、TLS 校验、轮数、超时、可接受状态、正文标记或允许机房时，旧缓存自动失效；切换反代模式或本轮直连出口基准变化也会失效。只保存直连基准的哈希，不保存基准地址明文。反代模式的通过条目还保存观察到的出口 IP。修改并发数、端口筛选、抽样大小、地区配额、允许出口类型或风险阈值不会清空仍然适用的 Zone 缓存，后两项在缓存读取后重新过滤。缓存命中不会延长原来的有效期。

1.3.0 将 Zone 缓存格式升至 v3，首次升级时旧版 Zone 缓存重新验证，以区分入口模式和反代模式；IPinfo 缓存继续复用。

命中缓存的候选跳过本轮大批量 Zone 请求；**命中通过缓存且真正入选下载测速或直接输出的少量节点，仍先做实时 Zone 复验**。复验失败就更新失败缓存并跳过，不下载、不输出；分地区模式下机房国家变化时跳过当前分组，避免写错标签。关闭测速时，按缓存延迟依次挑选并复验到配额满足为止，入选者再按刷新后的延迟排序；这不等于测量了所有缓存候选的当前延迟。缓存用于减少重复探测，不保证第二轮 Zone 请求绝对为零。

实际验证期间约每 5 秒保存已完成结果，阶段结束或正常处理中断时也保存，使用原子文件替换。损坏或过期的条目按未命中处理，未完成验证的候选不会被记成通过。缓存写入失败会明确警告，且在报告中记录 `write_errors`，当前已完成的校验结果仍可用于本轮运行。

日志会显示缓存通过/失败命中数、本轮实际验证量和下载/输出前复验量；报告的 `zone_cache` 保留相应计数。切换网络后需强制重测，可临时设置 `cache_enabled: false` 或删除 Zone 缓存文件，无需清理 IPinfo 缓存；兼容 `cache_ttl_hours: 0` 的旧用法。默认缓存路径在 `.state/` 下，不会进入 GitHub 同步。

`latency_ms` 是建立连接到收到 HTTP 响应头的耗时中位数，包含 HTTPS 握手；`jitter_ms` 为多轮响应头延迟的总体标准差。它不是单独测得的 ICMP 或纯 TCP RTT。TUN / VPN 接管的系统路由仍会影响本机的网络测试。

### 哪些阶段会再次发请求

| 阶段 | 缓存或复用方式 | 何时发起请求 |
| --- | --- | --- |
| IPinfo Lite 校准 | 按唯一 IP 缓存成功结果，默认 7 天 | 本次需要的 IP 缺少有效缓存时；同一 IP 的多个端口不重复查询 |
| TCP 测试 | 不缓存 | 每轮对端口和地区过滤后的全部候选建立 TCP 连接，再按延迟取前 N 个进入 Zone，不发 HTTP |
| 批量 Zone 验证 | 按 IP、端口和验证规则缓存，通过默认 6 小时、失败默认 15 分钟 | 无有效缓存时；有效失败缓存直接跳过，有效通过缓存先进入候选 |
| 下载/输出前 Zone 复验 | 本轮刚实际验证通过的节点复用本轮结果 | 使用通过缓存、且纳入测速名单或直接输出的节点按 `zone.rounds` 实时复验 |
| 反代直连基准 | 不持久缓存 | 仅 `proxyip.enabled: true` 且有入选候选时，每轮运行分别通过正常 DNS 请求 IPv4/IPv6，最多 `2 × baseline_rounds` 次 |
| 出口类型过滤 | 复用本轮或 Zone 缓存中的 trace 出口 | 仅反代模式执行，不额外请求候选；缓存节点复验后重新分类 |
| 出口风险查询 | 按唯一实际出口缓存成功评分，默认 7 天 | 仅反代与风险两个开关都开启时；类型过滤后的出口缺少有效风险缓存才请求 ipapi.is，新出口重新检查 |
| 下载测速 | 不保存测速结果缓存 | 仅 `speed.enabled: true` 时，通过前序检查、进入测速的候选重新下载 |

这里的“一次验证”不等于“一次 HTTP 请求”。默认两轮时，一个完整验证通过的候选会发起两次 GET，失败可能提前结束；测速前复验也按相同轮数执行。下载还会按连接数、重复次数和响应长度发起独立请求，因此启用 Zone 缓存不表示后续运行没有网络请求。

## 可选反代验证、出口类型和 IP 风险过滤

**`proxyip.enabled` 是总开关，默认 `false`。** 关闭时不执行反代出口变化判断、出口类型过滤和风险过滤，不读取风险密钥或风险缓存，也不发风险 API 请求；即使保留 `exit_types` 和 `risk.enabled: true`，这些子设置仍全部跳过。普通 Cloudflare 入口 IP 优选应保持关闭。

开启后按以下顺序处理，Zone 仍保留原来的缓存开关、成功/失败有效期和复验规则：

```text
取得本机 IPv4/IPv6 直连出口基准
→ Zone/trace 多轮校验，并检查每轮出口是否变化
→ 出口类型过滤
→ 风险过滤（risk.enabled=true 才执行）
→ 可选下载测速 → 排序、输出 → 可选推送
```

配置示例：在 `config.json` 顶层添加 `proxyip`。此例开启反代验证，只保留观察到 IPv4 出口的节点（包含观察到的双栈），并允许最高“轻微风险”：

```json
{
  "proxyip": {
    "enabled": true,
    "baseline_url": "https://www.cloudflare.com/cdn-cgi/trace",
    "baseline_rounds": 2,
    "exit_types": ["ipv4_only", "dual_stack"],
    "risk": {
      "enabled": true,
      "provider": "ipapi.is",
      "key_file": "ipapi_key.txt",
      "max_level": "轻微风险",
      "unknown_policy": "reject",
      "cache_enabled": true,
      "cache_file": ".state/ip_risk.json",
      "cache_ttl_hours": 168,
      "concurrency": 4,
      "timeout_seconds": 6,
      "retries": 2
    }
  }
}
```

### 反代出口变化判断

- 必须使用 `zone.path: "/cdn-cgi/trace"`，原有 trace 字段、TLS、状态和机房检查继续执行。候选每轮解析到的 `ip=` 都必须不同于相同地址族的直连基准。不同轮次可以使用同一个反代出口，不要求每轮换出口。
- `baseline_url` 须为 HTTPS trace 域名地址，默认 Cloudflare 官方域名。通过正常 DNS 分别强制 IPv4/IPv6，每族依次请求 `baseline_rounds` 次；不固定到候选 IP、不读取 HTTP 代理，使用正常证书校验。基准只在本轮内存中保存，可收集每个地址族的多个直连地址。
- IPv4 出口只与 IPv4 基准比较，IPv6 同理。某个地址族无法取得基准时，该族出口判为无法确认并拒绝；两个地址族都取不到则终止本轮。不会因为 IPv4 和 IPv6 的字符串不同而判为反代通过。
- 候选的出口判断复用已有 Zone 请求，仍由 `zone.rounds` 控制，不另加一批候选请求。新增请求是每轮运行最多 `2 × baseline_rounds` 次直连基准请求；失败的候选可提前结束。
- 这是 cfnb 风格的 HTTP 出口变化筛选。多出口、IPv6 隐私地址、VPN/TUN 等可能影响判断；不能代替真实 EdgeTunnel、WebSocket、VLESS 或其他隧道协议的连接测试，也不保证客户端可用。

### 出口类型

类型根据**本次完整验证各轮 trace 的出口 IP 集合**分类，与候选本身是 IPv4 还是 IPv6 无关：

| `exit_types` | 保留对象 |
| --- | --- |
| `[]` | 不限制类型 |
| `["ipv4_only"]` | 本轮仅观察到 IPv4 出口，不含观察到的双栈 |
| `["dual_stack"]` | 本轮同时观察到 IPv4 和 IPv6 出口 |
| `["ipv6_only"]` | 本轮仅观察到 IPv6 出口 |
| `["ipv4_only", "dual_stack"]` | 本轮观察到 IPv4 出口的所有候选 |

**这些名称表示观察结果，不是完整协议能力认证。** 只看到 IPv4 不证明节点不支持 IPv6；同理，看到两种出口也不保证任意目标的双栈访问。要观察双栈，`zone.rounds` 至少为 2，但增加轮数不保证一定出现两种出口。没有额外的类型探测请求。

### 出口风险

风险过滤仅在 `proxyip.enabled: true` 和 `proxyip.risk.enabled: true` 同时满足时执行，而且在出口类型过滤之后。查询对象是 trace 中**实际出口 IP**，不查询候选入口地址。一个候选出现多个出口，所有出口都必须满足阈值；共享出口的不同候选、不同端口同轮只查询一次。

1. 在 [ipapi.is](https://ipapi.is/) 获取该服务自己的 API 密钥，将 `ipapi_key.txt.example` 复制为 `ipapi_key.txt`，填写一条有效密钥。它与 IPinfo Lite Token 是两个服务的凭据，不能混用，也不使用环境变量。相对路径基于配置目录。
2. 有未缓存出口时才读取密钥；缺失或有多条有效密钥会明确报错，不尝试匿名查询。密钥通过 HTTPS POST 的 JSON 正文发送，不放入 URL；不跟随重定向，不写日志、报告或缓存。接口参数参考 [ipapi.is 官方文档](https://ipapi.is/developers.html)。
3. 沿用 cfnb 的五档自定义等级：`极度纯净 → 纯净 → 轻微风险 → 高风险 → 极度危险`。`max_level` 是允许的最高等级，默认“轻微风险”保留前三档。计算方式为 company/asn 滥用评分均值乘 5，加上 crawler/proxy/vpn/tor/abuser 每项 0.15，再乘 100；分界为 0.25 / 5 / 20 / 100。Bogon 直接判为极度危险。该等级不是接口官方的安全保证。
4. IP 不匹配、缺字段、无效评分、网络错误或 API 拒绝均为“未知”，不会把缺失评分当成 0。`unknown_policy: "reject"` 默认剔除；`"keep"` 明确保留未知，报告保留原因。401/403/429 后本轮停止新查询，已在途的请求可能完成；其余出口使用未知策略。临时网络错误和 5xx 最多额外重试 `retries` 次。
5. 成功评分按唯一出口 IP 缓存到 `.state/ip_risk.json`，默认 168 小时，读缓存不延长 TTL。`cache_enabled: false` 或 `cache_ttl_hours: 0` 不读写持久缓存，但同轮仍去重；未知结果只在同轮复用，下轮重新尝试。改 `max_level` 或未知策略立即重新过滤，不需要清空 Zone 缓存或风险缓存。`concurrency` 控制同时查询的不同出口数，`timeout_seconds` 是每次 API 请求总超时。

缓存通过的节点在下载/输出前实时复验时，会重新收集本次出口、分类并执行风险过滤。出现新出口就查新出口的风险缓存，缺失则查询 API；不沿用旧出口的风险结论。类型或风险失败就跳过该候选，全部被过滤时保留旧 `ip.txt`、返回失败并且不推送，不自动放宽阈值。

`ip.txt` 继续保持原来的地区标签和可选测速字段，不额外塞入出口或风险字段。`report.json` 的节点增加 `proxy_exit_ips`、`proxy_exit_type` 和 `proxy_risks`，报告也记录反代开关、基准可用地址族、风险查询/缓存计数及过滤原因。反代模式下 Zone 缓存、风险缓存和报告会保存实际出口地址；不保存完整 trace 或本机直连基准明文。`ipapi_key.txt`、`.state/` 和 `report.json` 均默认忽略。

## 测速方式

`speed.enabled: false` 完全关闭下载测速，仍执行 TCP 和 Zone，按 Zone 延迟选取。`speed.min_mbps`、`streams`、`repeats` 和测速名单数量此时不参与执行，`max_latency_ms` 仍有效。`ip.txt` 不显示未实测的速度，`report.json` 用 `speed_enabled: false` 标明此轮没有测速；节点的 `speed_samples` 为空。

先根据实际部署选择模式和对应的下载地址：

| 使用场景 | `speed.mode` | `speed.url` 示例 |
| --- | --- | --- |
| 使用默认 Cloudflare 下载端点 | `static` | `https://speed.cloudflare.com/__down?bytes=100000000` |
| 使用已部署测速功能的 CFIP 或兼容 Worker | `worker` | `https://cdn.your-domain.com/__speed` |

**`/__down` 和 `/__speed` 由对应服务实现，不是任意域名都支持的通用下载路径。** 使用自己的 Worker 时，应填写其实际实现的路径，不能只把默认 URL 的域名替换成自己的域名并保留 `/__down`。`worker` 模式要求 URL 的域名与 `zone.domain` 相同，并检查回源确认头。

默认 `speed.mode: "static"` 使用配置里的静态下载 URL，用于比较本地到目标 CDN 的下载能力：

```json
{
  "speed": {
    "enabled": true,
    "mode": "static",
    "url": "https://speed.cloudflare.com/__down?bytes=100000000",
    "streams": 4,
    "repeats": 3,
    "concurrency": 1,
    "connect_timeout_seconds": 3,
    "timeout_seconds": 8,
    "duration_seconds": 5,
    "max_bytes": 20000000,
    "min_bytes": 65536,
    "candidates_per_region": 12,
    "min_mbps": 0,
    "max_latency_ms": 0,
    "sort_by": "speed"
  }
}
```

每个地区先按 Zone 验证延迟选择测速名单。`candidates_per_region` 非零时，名单上限为它与该地区目标配额中的较大值；实际候选不足时只取现有数量，设为 0 则纳入该地区全部通过候选。命中通过缓存的入选节点在下载前实时复验。**此项是有限的测速预算，不保证在未测速候选中找到全局最快者；复验、延迟检查或测速失败后不会从名单外自动补测。**

不同 IP 默认串行测速，`speed.concurrency` 可设置同时测速的 IP 数；各地区分组依次处理。单个 IP 内支持 1～16 路连接。支持 `Range`、HTTP keep-alive 和 chunked 响应，结束后仍有时间/字节预算就复用连接继续请求，所以一个下载流不保证整轮只有一个 HTTP 请求。下载链接的主机名用于 SNI/Host，端口被替换为当前候选端口，连接始终固定到候选 IP。测速不跟随重定向，须配置最终下载 URL。

每次测量有总时长和跨连接共享的正文接收字节上限。速度用计入的有效下载字节除以实际总耗时，包含连接和请求开销，单位为十进制 Mbps。拒绝 HTML/JSON/XML 错误页、非 200/206、压缩响应、过短正文及传输错误，不能把几百字节的错误页累加成“高速”。每轮必须有效，再取重复测量的中位数。`max_bytes × repeats × 每地区测速候选数` 可估算该地区的下载正文预算，各地区相加才是本轮总预算；网络缓冲可能让实际线路字节略多。

按上述示例，单个候选每轮开 4 条连接，共享 5 秒下载时间预算和 20,000,000 字节（20 MB）正文预算，达到其中一项就结束本轮；这是 4 条连接合计的预算，不是每条连接各下载 20 MB。成功测满 3 轮时正文预算合计最多 60 MB，再取三轮速度中位数。任意一轮失败就淘汰该候选，不会拿剩余成功轮次凑结果。

- `min_bytes`：最小有效正文要求，默认 65,536 字节，用于拒绝过短响应。
- `min_mbps: 0`：关闭最低速度门槛，仍会实际下载和检查响应有效性。
- `max_latency_ms: 0`：关闭延迟上限；大于 0 时在下载前根据 Zone 响应延迟过滤。
- `sort_by: "speed"`：最终按速度从高到低排序，同速时优先低延迟；`"latency"` 改为延迟优先，同延迟时优先高速度，仍要求完成测速。

如果日志提示“单次下载正文不足：收到 X 字节，最低要求 Y 字节，本轮测速无效”，表示某次 HTTP 响应实际接收的正文不足 `min_bytes`，仅凭长度不能确认它是错误页。这里检查的是单次响应；某条连接因此被判无效时，当前候选的本轮测速仍按原规则失败，其他连接收到足够数据也不会抵消这个失败。

### 可选：Worker 回源测速

如果已有 CFIP 或兼容 Worker，且 `/__speed` 已能返回二进制数据和 `X-Upstream-Mode`、`X-Upstream-Landed` 两个确认头，直接按下面的 JSON 设置本地配置即可。`/__speed` 不需要附加默认下载端点的 `?bytes=100000000`；本地下载预算由 `duration_seconds` 和 `max_bytes` 控制。

如果需要新部署，仓库附带 `worker/speed-worker.js`，这是独立 Worker 示例，也提供可整合到已有 Worker 的 `handleSpeed()`。以下部署步骤适用于这个附带示例：

1. 在 Worker 中设置 `UPSTREAM_URL`，指向你有权下载、足够大的 HTTPS 二进制文件，选能正常返回 200/206 的稳定源。
2. 默认不设置测速口令，直接访问 `/__speed` 即可。只有在 Worker 端启用了可选的 `SPEED_TOKEN` 认证时，才需要在本地 `speed_token.txt` 中填写相同口令。
3. 将 Worker 绑定到自己的域名。整合到已有 Worker 时，在原 `fetch` 最前调用 `const result = await handleSpeed(request, env); if (result) return result;`。
4. 本地工具只读文件，不需要设置环境变量。改配置：

```json
{
  "zone": {
    "domain": "cdn.your-domain.com"
  },
  "speed": {
    "mode": "worker",
    "url": "https://cdn.your-domain.com/__speed",
    "token_file": "speed_token.txt"
  }
}
```

这里只展示需要设置的字段，修改时保留现有 `zone` 和 `speed` 中的其他参数。

`speed_token.txt` 只填写一行口令；文件不存在、为空、只有注释，或将 `speed.token_file` 设为 `""`，均表示不发送口令。有口令时客户端同时支持 `token` 查询参数及 `X-Speed-Token` 请求头；附带 Worker 两种方式均可验证。`static` 模式不会读取测速口令文件。安装脚本只在文件不存在时复制空白示例。

Worker URL 必须与 `zone.domain` 相同。客户端检查 `X-Upstream-Mode` 和 `X-Upstream-Landed`，缺少它们就判为无效，避免把普通网页当回源测速结果。连接固定到候选 IP 和端口，以 Worker 域名作为 SNI/Host；测得的是本机经该候选接入 Worker、再由 Worker 回源的整体下载表现。该口径包含所选 Worker 与上游路径，不能与静态源数字直接等同；也不代表任意目标网站的通用速度。

### Zone 通过，但测速提示 `text/html`

`拒绝错误页类型 text/html` 表示下载请求收到了 HTML 页面；出现这条提示时，HTTP 状态已是 200 或 206，但正文类型不合格。优先核对 `speed.mode` 和完整的 `speed.url`：

- 自建 Worker 实现的是 `/__speed` 时，使用 `mode: "worker"` 和对应的 `/__speed` 地址。
- 检查是否误填了首页、登录页、默认回退页面，或请求遇到了验证页。
- Zone 默认请求 `zone.path: "/cdn-cgi/trace"`（已有配置保留原路径），下载请求 `speed.url`；前者通过不能代替后者的下载有效性检查。

先确认测速地址及其实际响应，再重跑优选；增加候选数量或关闭错误页检查无法修正测速路径。`check` 只检查配置结构，不会请求这两个路径，也不会验证下载内容。

## GitHub 同步与确认

GitHub 认证从 Git 配置读取，不使用环境变量，也不读取 GitHub Token 文本文件。HTTPS 推送可用一条命令持久保存 Token：

```bash
git config --local cfscan.githubToken "你的 GitHub Token"
```

该值以明文保存在当前仓库的 `.git/config` 中，重新登录或重启后仍有效。它是本工具读取的 Git 配置项；仅对 GitHub HTTPS 推送及远端确认请求使用。已有 Git 凭证助手或 SSH 认证时，可以不设置此项。`user.name` / `user.email` 只负责提交署名，不代替认证。

首次使用且目标是新建的空 GitHub 仓库时，在工具目录执行以下命令。仓库地址需要替换成自己的，署名和邮箱也可自定义：

```bash
git init -b main
git config --local user.name "CFSCAN"
git config --local user.email "cfscan@local.invalid"
git remote add origin https://github.com/your-name/your-repo.git
git config --local cfscan.githubToken "你的 GitHub Token"
```

不需要上传工具源码或手工创建空提交，程序会在有结果时为 `ip.txt` 创建提交。已经有 Git 仓库时跳过初始化；远端已有历史时先与其对齐，不要创建冲突的独立历史。

在 `config.json` 中启用：

```json
{
  "github": {
    "enabled": true,
    "remote": "origin",
    "branch": "main"
  }
}
```

`branch` 留空使用当前分支。程序只为输出路径建立提交，用户已暂存的其他修改保持暂存状态。已有的本地提交历史会随正常 Git 推送发布，请使用专用结果仓库或先确认当前仓库历史。

成功必须同时满足：

1. 输出文件对应内容确实存在于本地 `HEAD`。
2. `git push --porcelain` 的进程退出码为 0。
3. 向实际 **push URL** 单独执行 `git ls-remote`，确认目标分支 SHA 等于本地提交 SHA。
4. 推送期间本地结果没有再次变化。

不会依赖 Shell 脚本打印的“成功”文字，也不会把旧成功状态沿用到新任务。任何未确认成功的情况都会写 `.state/github_sync.json`，并返回非零退出码；被拒绝的提交下次即使文件没变化仍会继续尝试推送。不强制推送、不自动重置仓库；远端有冲突或分支保护时明确失败，需先处理 Git 历史或权限。

已生成结果但 GitHub 推送失败时，可单独重试：

```bash
.venv/bin/python main.py sync
```

Windows 对应命令为 `.\start.cmd sync`。正常优选并自动同步时，Linux 使用 `.venv/bin/python main.py`，Windows 使用 `.\start.cmd`。

`config.json`、`ipinfo_tokens.txt`、`speed_token.txt`、`ipapi_key.txt` 和 `.state/` 默认忽略，`ip.txt` **不忽略**，以支持正常同步。`.git` 不会包含在发布压缩包里。

从旧版本升级可保留已有配置、Token、`.state/` 和 `.git`，覆盖程序文件即可。不要用示例配置覆盖自己的域名和 Token。旧的 `token_env` 字段会被忽略，可从 `ipinfo`、`speed`、`github` 三个对象删除；`speed.token_file` 缺省为 `speed_token.txt`。

1.2.0 新增字段缺失时：`regional_mode: true`、`top_n: 15`、TCP 并发 300 / 1 轮 / 3 秒 / 成功率 1.0、`zone.cache_enabled: true`、`speed.enabled: true`、测速 IP 并发 1、下载连接超时 3 秒。已有 Zone 及测速轮数/连接数等显式设置保留，Zone 缓存有效期设置保留。

**1.2.0 流程变化：全局随机抽样也移到 Lite 校准和地区筛选之后，全部剩余候选新增 TCP 初筛。** 首次 Lite 校准量可能比旧的“先全局抽样”模式更多，后续复用 Lite 缓存；TCP 不会调用 Worker，但运行时会多建立一些连接。原有 config 不会自动增加说明字段，可对照新的 `config.example.json` 手动补充。

**1.3.0 新增可选反代验证及出口过滤。** 旧配置缺省 `proxyip.enabled: false`，保持普通入口优选；出口类型和风险过滤只在开启反代后执行，风险还需独立开启。需要时参照 `config.example.json` 手工添加配置。Zone 缓存升级到 v3 后首次重新验证；Lite 缓存和现有 Token/Git 设置保留。发布包新增的是 `ipapi_key.txt.example`，不包含实际密钥。

**1.2.1 将随机抽样替换为 TCP 延迟前 N 筛选。** 顺序是地区筛选后先对全部剩余候选做 TCP 测试，再按延迟升序取前 N 个进入 Zone。现有 `candidates.sample_size` / `sample_scope` 名称和值继续使用，含义变为“前 N 个 / 全局或各地区”，无需迁移字段。`sample_size: 0` 仍表示不限数量；同延迟按 IP:端口固定排序。TCP 成功率仍先过滤，入选名次不按成功次数优先。Zone 缓存设置及后续测速、推送行为不变。

升级至 1.1.2 后，如要减少 Worker 验证请求，还需将已有配置的 `zone.path` 改为 `/cdn-cgi/trace`，并将首页专用的 `zone.expected_body` 清空。未填写路径的配置使用新的 trace 默认值；明确配置的自定义路径继续保留。

## 输出、报告与退出码

`ip.txt` 为 UTF-8 无 BOM，每行一个节点，包含地区代码和中文名称。以下是格式示例，文档 IP 不可直接使用：

```text
192.0.2.1:443#SG 新加坡 86ms 45.67Mbps colo=SIN
[2001:db8::1]:443#JP 日本 112ms 31.20Mbps colo=NRT
```

设置 `output.include_metrics=false` 后只输出 `IP:端口#代码 中文名`。关闭测速时，即使开启 metrics 也仅显示延迟和可用的 colo，不显示 Mbps。报告包含候选过滤及 TCP 前 N 选取数量、TCP 通过数及节点成功次数/延迟/抖动、Zone 缓存计数、分地区或全局目标数、实测次数、缺额、剔除原因、各轮速度及推送状态。使用原子文件替换，整轮无结果保留旧 `ip.txt`；部分结果输出实际通过数量，不混入旧文件的节点。

程序默认将日志输出到终端，不自动生成日志文件；`report.json` 是结构化报告，不是终端日志副本。Linux 如需同时显示并保存日志：

```bash
mkdir -p logs
set -o pipefail
.venv/bin/python main.py --debug 2>&1 | tee -a logs/cfscan.log
```

`--debug` 用于显示主程序捕获的异常调用栈，不会把所有日志改成 DEBUG 级别。默认 `.gitignore` 会忽略 `*.log`。

| 退出码 | 含义 |
| --- | --- |
| 0 | 本地结果成功；启用同步时也已确认远端；或 init/check 成功 |
| 1 | 配置、网络、输入或验证失败 |
| 2 | 本地结果存在，但 GitHub 未确认成功 |
| 130 | 用户中断 |

OS 文件锁阻止同一配置目录重叠运行，进程异常退出会自动释放，可配合计划任务或 cron 使用。

## 开发与验证

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
.\.venv\Scripts\ruff.exe check .
```

测试使用本地 TCP/HTTP/TLS 服务和临时 bare Git 仓库，涵盖流程顺序、全部 TCP 后全局/每地区按延迟取前 N、同延迟固定排序、TCP 成功率/并发/取消清理、真实 SNI/Host、trace 响应一致性、缓存开关、关闭测速、分地区/全局配额、下载并发、chunked/keep-alive、错误页与超时，反代总开关与子过滤门控、相同地址族出口比较、反代缓存隔离、新出口复验过滤、风险 API 去重/限流/未知策略/密钥隔离，以及远端拒绝、SHA 不匹配、push/fetch URL 不同和失败重试。测试不发布到真实 GitHub。

## 参考与接口

- 架构参考：[xinyitang3/cfnb](https://github.com/xinyitang3/cfnb)。
- 验证与多流测速设计参考：[xinerqu/CFIP](https://github.com/xinerqu/CFIP)。该仓库当前提供 README 和发布资产，本项目独立实现 Python 流程。
- [IPinfo Lite 官方文档](https://ipinfo.io/developers/lite-api)。
- [Cloudflare 错误 1034 说明](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1034/)。

第三方候选源可能不可用；可替换成自己的本地列表或其他来源。
