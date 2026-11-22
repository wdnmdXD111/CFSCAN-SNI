# CFSCAN-SNI

本地运行的 Python Cloudflare 优选 IP 工具。沿用 cfnb 的“本地配置 + 多候选源 + 校准缓存 + 结果文件 + 自动同步”结构，采用 CFIP 的先验证、后测速思路，新增两种地区口径和可确认的 GitHub 推送状态。

待更新：zone校验默认改为GET /cdn-cgi/trace

现版本建议手动更改GET /cdn-cgi/trace

GET /cdn-cgi/trace不消耗worker次数，因此可以把zone缓存关闭

```text
读取配置并加运行锁 → 聚合候选文件/URL 并按 IP:端口去重 → 按端口过滤
    ↓
可选全局随机抽样 → IPinfo Lite 校准并缓存（两种地区口径都经过此阶段）
    ↓
IP 国家模式：先按 Lite 国家筛选 → 可选每地区随机抽样 → Zone 缓存 / 实际验证
机房国家模式：Zone 缓存 / 实际验证 → 从 CF-Ray 机房归类国家
    ↓
每个地区按响应延迟选择测速候选 → 命中通过缓存的入选节点进行实时 Zone 复验
    ↓
逐 IP 测试：多连接下载及正文有效性检查 → 全部重复轮次有效后取速度中位数
    ↓
每个地区分别 Top N → 原子替换 ip.txt → 可选 GitHub 提交、推送并读取远端 SHA 确认
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
2. **聚合候选并去重。** 从 `sources` 读取文件或 URL，解析为 `IP:端口`；没写端口时使用 `default_port`。同一 IP 的不同端口分别验证和测速。CIDR 按 `cidr_samples` 固定抽样，默认每个网段最多取 64 个地址。`source_max_candidates` 在此阶段检查，发生在端口过滤和随机抽样之前。
3. **过滤端口。** 只保留 `candidates.ports` 允许的候选，`[]` 表示不限端口。全量和抽样模式都执行此步骤，并且只筛选已有端口，不改写端口。
4. **按抽样模式安排 Lite 校准和地区筛选。** 在 `ipinfo` 地区口径下，`global` 先全局随机抽样，再做 Lite 校准和目标地区筛选；`per_region` 先做 Lite 校准、筛选目标国家，再从每个国家分别随机抽样。`sample_size: 0` 不抽样。Lite 先查缓存，只有缺失或过期的唯一 IP 才查询 API；默认跳过校准失败的 IP，`require_all: true` 时存在校准失败就终止本轮。`colo` 模式也会经过 Lite 校准，但最终在 Zone 之后按机房国家分组，开启抽样时只支持 `global`。
5. **检查 Zone 缓存或实际验证。** 有效的失败缓存直接跳过；有效的通过缓存进入下一阶段；无缓存或过期则连接候选 IP 和端口，以 `zone.domain` 作为 SNI/Host 请求 `zone.path`，检查 TLS、状态、Cloudflare 响应头和已识别的错误，以及可选正文标记、机房限制。默认验证两轮，保存通过或失败结果。
6. **按地区和延迟确定测速名单。** 在各地区的 Zone 通过候选中按响应延迟从低到高取一批，数量由 `speed.candidates_per_region` 和该地区输出配额决定。设为 `0` 才把该地区全部通过候选纳入测速名单。
7. **下载前复验和延迟过滤。** 名单中使用了 Zone 通过缓存的节点先做实时复验，复验仍按 `zone.rounds` 执行；本轮刚实际验证通过的节点不重复验证。复验失败、`colo` 分组下机房国家不再属于当前分组，或超出 `max_latency_ms` 限制时跳过该候选。
8. **逐个候选做多连接下载。** 不同 IP 串行，单个 IP 内按 `streams` 并行下载，每轮受时长和共享字节预算限制。重复 `repeats` 轮，全部有效后取速度中位数，再检查 `min_mbps`。错误页、异常状态、过短正文或传输错误会导致失败；Worker 模式额外检查回源确认头。测速结果不缓存。
9. **每个地区排序取配额并写文件。** 对测速成功的节点按 `sort_by` 排序，再取 `regions` 指定的数量。缺额时输出实际合格数量，不自动补抽、不从测速名单外补测，也不混入其他地区或旧结果。整轮没有任何合格节点时保留旧 `ip.txt`；有结果时原子替换文件，并写入报告。
10. **按需同步 GitHub。** 启用 `github.enabled` 且本次未指定 `--no-github` 时，提交输出文件、推送，并单独读取远端分支 SHA 确认结果。推送失败仍保留已生成的本地文件，失败状态可用于后续重试。

### 三种数量分别控制什么

| 参数 | 控制阶段 | 示例含义 |
| --- | --- | --- |
| `candidates.sample_size` | 进入后续验证流程的抽样范围 | `1000`：全局最多抽 1000 条，或每个目标国家最多抽 1000 条，由 `sample_scope` 决定；`0` 表示不抽样 |
| `speed.candidates_per_region` | 每个地区纳入下载测速名单的数量 | `12`：优先测延迟最低的 12 条；输出配额大于 12 时取配额数量，实际候选不足则取现有数量；`0` 表示测全部通过候选 |
| `regions` | 最终输出数量 | `{"HK": 10, "JP": 10}`：香港、日本分别最多输出 10 条合格结果 |

例如，某地区 Zone 通过 500 条，`candidates_per_region: 12`、输出配额为 10 时，先取延迟最低的 12 条作为测速名单，再从测速成功者中选最多 10 条。**最终结果来自这 12 条的比较，不代表已经测完其余 488 条。** 如果其中只有 8 条通过，就输出 8 条并报告缺额。

## 必填配置与按地区运行

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

源文件原标签不作为最终国家依据；通过端口过滤和可选全局抽样后，进入后续流程的 IP 经过 Lite 校准。IP+端口去重，同一个 IP 的地区信息只查询一次。

默认不随机截断具体 IP，可按下一节显式启用抽样。CIDR 本身没有地区信息，程序通过 `cidr_samples` 对每个网段做固定、分层抽样，IPv6 大网段也不会展开成巨量地址。`source_max_candidates` 是聚合候选时的总量保护，在端口过滤和抽样之前检查，超过就报错；需要接收更大的源时应相应提高此上限。

## 端口过滤与随机抽样

在 `config.json` 顶层加入 `candidates`。例如，只从 443 端口的候选中随机取最多 1000 条，再执行 IPinfo、Zone 和测速流程：

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
- `sample_size`：抽样上限，必须是非负整数；默认 `0` 表示全量处理过滤后的候选。每条 `IP:端口` 算一个候选，同一 IP 的不同端口分别计数，抽样无放回。候选不足时取现有数量，不重复抽、不自动扩大预算来填满地区配额。
- `sample_scope: "global"`：对整个端口过滤后的池抽取最多 `sample_size` 条，**在 IPinfo 查询之前**执行，节省校准请求及后续 Zone 请求。抽样后仍按 `regions` 独立优选，但不能保证每个地区都抽到足够候选。
- `sample_scope: "per_region"`：先对端口过滤后的池做 Lite 校准和国家筛选，再从每个目标国家最多抽取 `sample_size` 条进入 Zone。适用于 `region_basis: "ipinfo"`，可避免候选多的国家挤占其他国家预算。首次 IPinfo 仍需校准过滤后的整个池，之后复用 Lite 缓存。

全量只测 443 时使用 `"ports": [443], "sample_size": 0`。若每个国家各随机取 100 条，则使用 `"sample_size": 100, "sample_scope": "per_region"`；抽样数量与 `regions` 的最终输出数量是两回事。

`region_basis: "colo"` 需要先连接才能知道当前机房国家，因此开启抽样时只支持 `global`，不能根据 IPinfo 国家冒充机房国家提前分地区抽样。随机抽样是减少请求量的预算选择，不保证全候选中的全局最优。日志和 `report.json` 的 `candidate_pool` 会记录过滤前后、IPinfo 前、Zone 前以及各地区的抽样数量。

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

- 验证正文里的 Cloudflare 错误，例如 1034、1033、1016、1020，同时处理纯文本、HTML 标签和常见 JSON 形式。
- 验证可接受 HTTP 状态及 Cloudflare 响应头；普通 403 默认也不会通过。
- `zone.expected_body` 可设置自己页面的特征文本，进一步确认确实访问到了自己的服务。只检查响应正文前 64 KiB。
- 不自动跟随验证重定向，避免跳到另一个 IP 后误判；跳转站可把 `zone.path` 改成明确的探测路径。
- 全部验证失败不会回退为“全部有效”。

默认 `zone.rounds: 2`、`zone.concurrency: 48`：单个候选需要完成两轮验证，最多并行处理 48 个候选。**Zone 通过只说明验证域名和路径满足规则，测速 URL 仍需返回合格的下载正文。** 修改 `zone.domain` 不会自动修改 `speed.url`。

### Zone 结果缓存

Zone 验证结果默认保存到配置目录下的 `.state/zone_cache.json`。可在现有 `zone` 对象内设置：

```json
{
  "zone": {
    "cache_file": ".state/zone_cache.json",
    "cache_ttl_hours": 6,
    "cache_failure_ttl_hours": 0.25
  }
}
```

- `cache_file`：缓存文件路径，相对路径以配置文件所在目录为准。
- `cache_ttl_hours`：通过结果的有效期，默认 6 小时；设为 `0` 关闭整个 Zone 缓存，恢复每轮实际验证。
- `cache_failure_ttl_hours`：失败结果的有效期，默认 0.25 小时（15 分钟）；设为 `0` 则不缓存失败，仍可缓存通过结果。临时连接故障不会沿用与通过结果同样长的有效期。

缓存以 **IP + 端口 + Zone 校验规则** 区分，包含通过/失败原因、机房和延迟信息。修改域名、路径、HTTP/HTTPS、TLS 校验、轮数、超时、可接受状态、正文标记或允许机房时，旧缓存自动失效；因此改正错误域名后不会继续命中旧域名的失败结果。修改并发数、端口筛选、抽样大小和地区配额不会清空仍然适用的缓存。缓存命中不会延长原来的有效期。

命中缓存的候选跳过本轮大批量 Zone 请求；**命中通过缓存且真正入选下载测速的少量节点，仍先做实时 Zone 复验**。复验失败就更新失败缓存并跳过，不下载、不输出；机房国家发生变化时跳过当前国家分组，避免写错标签。缓存延迟用于选择本轮测速候选，入选节点会由复验刷新延迟。因此缓存用于减少重复探测，不保证第二轮 Zone 请求绝对为零。

实际验证期间约每 5 秒保存已完成结果，阶段结束或正常处理中断时也保存，使用原子文件替换。损坏或过期的条目按未命中处理，未完成验证的候选不会被记成通过。缓存写入失败会明确警告，且在报告中记录 `write_errors`，当前已完成的校验结果仍可用于本轮运行。

日志会显示缓存通过/失败命中数、本轮实际验证量和测速前复验量；报告的 `zone_cache` 保留相应计数。切换网络后若需要立即强制重测，可删除对应 Zone 缓存文件或临时设置 `cache_ttl_hours: 0`，无需清理 IPinfo 缓存。默认缓存路径在 `.state/` 下，不会进入 GitHub 同步。

`latency_ms` 是建立连接到收到 HTTP 响应头的耗时中位数，包含 HTTPS 握手；`jitter_ms` 为多轮响应头延迟的总体标准差。它不是单独测得的 ICMP 或纯 TCP RTT。TUN / VPN 接管的系统路由仍会影响本机的网络测试。

### 哪些阶段会再次发请求

| 阶段 | 缓存或复用方式 | 何时发起请求 |
| --- | --- | --- |
| IPinfo Lite 校准 | 按唯一 IP 缓存成功结果，默认 7 天 | 本次需要的 IP 缺少有效缓存时；同一 IP 的多个端口不重复查询 |
| 批量 Zone 验证 | 按 IP、端口和验证规则缓存，通过默认 6 小时、失败默认 15 分钟 | 无有效缓存时；有效失败缓存直接跳过，有效通过缓存先进入候选 |
| 测速前 Zone 复验 | 本轮刚实际验证通过的节点复用本轮结果 | 使用通过缓存、且纳入测速名单的节点按 `zone.rounds` 实时复验 |
| 下载测速 | 不保存测速结果缓存 | 每轮运行中通过前序检查、实际进入测速的候选都会重新下载 |

这里的“一次验证”不等于“一次 HTTP 请求”。默认两轮时，一个完整验证通过的候选会发起两次 GET，失败可能提前结束；测速前复验也按相同轮数执行。下载还会按连接数、重复次数和响应长度发起独立请求，因此启用 Zone 缓存不表示后续运行没有网络请求。

## 测速方式

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
    "mode": "static",
    "url": "https://speed.cloudflare.com/__down?bytes=100000000",
    "streams": 4,
    "repeats": 3,
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

不同 IP 串行测速，避免互抢本机带宽；单个 IP 内支持 1～16 条连接。支持 `Range`、HTTP keep-alive 和 chunked 响应，结束后仍有时间/字节预算就复用连接继续请求。下载链接的主机名用于 SNI/Host，端口被替换为当前候选端口，连接始终固定到候选 IP。测速不跟随重定向，须配置最终下载 URL。

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
- Zone 默认请求 `zone.path: "/"`，下载请求 `speed.url`；前者通过不能代替后者的下载有效性检查。

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

`config.json`、`ipinfo_tokens.txt`、`speed_token.txt` 和 `.state/` 默认忽略，`ip.txt` **不忽略**，以支持正常同步。`.git` 不会包含在发布压缩包里。

从旧版本升级可保留已有配置、Token、`.state/` 和 `.git`，覆盖程序文件即可。新增字段缺失时自动采用默认值：端口不限、全量处理、Zone 通过缓存 6 小时、失败缓存 15 分钟。要启用端口筛选或随机抽样，按上面的示例向现有配置补充字段，不要用示例配置覆盖自己的域名和 Token 设置。旧的 `token_env` 字段会被忽略，可从 `ipinfo`、`speed`、`github` 三个对象中删除；`speed.token_file` 未写时默认使用 `speed_token.txt`。

## 输出、报告与退出码

`ip.txt` 为 UTF-8 无 BOM，每行一个节点，包含地区代码和中文名称。以下是格式示例，文档 IP 不可直接使用：

```text
192.0.2.1:443#SG 新加坡 86ms 45.67Mbps colo=SIN
[2001:db8::1]:443#JP 日本 112ms 31.20Mbps colo=NRT
```

设置 `output.include_metrics=false` 后只输出 `IP:端口#代码 中文名`。`report.json` 包含候选过滤和抽样数量、Zone 缓存计数、每个地区的目标数、Zone 通过数、实际测速数、测速通过数、缺额、被剔除原因、入选节点的各轮速度及 GitHub 同步状态。使用原子文件替换，整轮无结果时保留旧 `ip.txt` 并写失败报告；有部分结果时写实际通过的节点，并在报告中列出缺额，不混入旧文件中的其他节点。

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

测试使用本地 HTTP/TLS 服务和临时 bare Git 仓库，涵盖真实 SNI/Host、分阶段拒绝、分地区配额、chunked/keep-alive、错误页与超时，以及远端拒绝、SHA 不匹配、push/fetch URL 不同和失败后的再次推送。测试不发布到真实 GitHub。

## 参考与接口

- 架构参考：[xinyitang3/cfnb](https://github.com/xinyitang3/cfnb)。
- 验证与多流测速设计参考：[xinerqu/CFIP](https://github.com/xinerqu/CFIP)。该仓库当前提供 README 和发布资产，本项目独立实现 Python 流程。
- [IPinfo Lite 官方文档](https://ipinfo.io/developers/lite-api)。
- [Cloudflare 错误 1034 说明](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1034/)。

第三方候选源可能不可用；可替换成自己的本地列表或其他来源。
