# 跨区 DNS 变更发布顺序规划器与冷缓存回源估算

给内网 DNS 值班员用的 FastAPI 服务，包含两个离线接口：

* **`POST /plan`**：提交最多 8 个**互不嵌套**的权威区的新旧 A / CNAME 记录，
  以及必须持续可解析的业务域名，按**无缓存权威记录**推演每一次**整区切换**
  后的解析状态，判断能否安全发布。
* **`POST /simulate`**：递归解析器重启、缓存为空时，按给定查询时间线估算每个
  权威区会收到多少回源查询、集中在哪些秒，并给出每条查询最终的 IPv4 地址或
  失败原因。

## `/plan` 推演模型

* 每个区只有两个状态：整区旧记录、整区新记录。一次发布 = 一个区整区切换，
  区内不存在新旧共存的中间态。
* 解析不经过缓存，不使用 TTL；记录里的 `ttl` 只做原样保留，不影响结论。
* 支持 A 与 CNAME。CNAME 链跨已提交的区追踪，最多 32 跳。
* CNAME 链**离开已提交的区**即视为无法确认（`external_target`）。需要把外部区
  纳入推演时，把它按 `records == new_records` 一并提交即可。
* 所有记录必须属于本区；同一名称不能同时有 A 与 CNAME；区顶点不允许 CNAME；
  同名 CNAME 目标必须一致；A 值必须是 IPv4。违反则返回 422。

### 为什么不是贪心

安全谓词不单调：发布一个区可能同时修好一个域名、又弄坏另一个；一个“当前会断”
的状态也可能被后发布的区修好。因此“每次挑一个当前安全的区”并不保证找到存在的
安全顺序。服务对 **2^n ≤ 256 个状态**做完整深度优先搜索（带记忆化），
一定给出正确结论；无解时在所有等长的安全前缀中挑“卡点失败域名最少”的那个。

## `/simulate` 模拟模型

请求 = `/plan` 格式的区（额外要求每区一个固定的回源往返耗时 `latency_ms`，
正整数毫秒）+ 最多 500 条 A 查询（`at_ms` 为重启后的毫秒数）。所有查询共用
一个初始为空的缓存。

**时间与顺序**

* 回源耗时是**固定往返**：`t` 时刻发出的查询在 `t + latency_ms` 收到回包。
* 同一毫秒内的处理顺序：**先处理缓存过期，再处理回包，再处理新查找**。
  因此回包时刻开始的查询一定能用上刚写入的缓存。

**缓存**

* 缓存按**记录名**存放（校验保证一个名称不会同时有 A 与 CNAME）；只缓存
  A / CNAME 正向应答。
* 同一组记录的 TTL 取**最短 TTL**，计时从**收到回包**的时刻开始；到点即失效，
  下一次查找重新回源。
* **缺记录**的应答（NXDOMAIN / NODATA）**不写入缓存**，下一次查找仍会回源。
* 观测窗口内区数据不变，模拟只使用 `records`（重启后线上数据）；`new_records`
  仅为与 `/plan` 格式兼容而接受，不参与计算。

**并发与解析**

* 缓存未命中且同名回源**已在途**时不重复发包：所有等待者拿到**同一份回包**，
  然后各自从收到回包的时刻继续解析自己的 CNAME 链。
* 解析与断链规则与 `/plan` 完全一致（缺记录 NODATA/NXDOMAIN、成环、链出区、
  链过长），两边复用同一套判定与文案。

**统计口径**

* 回源按**发出时刻**归属到区：记一次数，并累加到所在秒（`second = at_ms // 1000`）。
* 每个区只列出**实际有回源**的秒；`peak` 是该区回源次数最多的秒，并列时取**最早**
  的那一秒。从未被回源的区秒列表为空、`peak.second` 为 `null`。
* 每条查询给出最终 IPv4 地址或失败原因，并附 `cache_hits`、`coalesced_waits`
  （等待在途回包的次数）、`upstream_sent`（该查询自身发出的回源数）。

**一处需要值班员知晓的实现选择**：6 个区以上、每条链路都很长时，缓存未命中的
查询只会在**它自己这一跳**决定是否发包；多个等待者拿到同一份回包后各自继续，
若下一跳仍然未命中，会各自发起查找。服务只对**真正同时发出**的同名回源去重，
不会把“已经收到回包、正在解析下一跳”的查询算作在途。这与“各查询收到结果后再
各自继续解析”的规则一致。

**取舍**：只有正向应答进缓存，负应答逐次回源，因此高频查询缺失域名时回源量
会明显高于真实递归解析器（后者通常有 RFC 2308 的负缓存）——这是偏保守的估算。
`/simulate` 也假定区数据在观测窗口内不变，因此不能用它推算“切换过程中”的
TTL 重取时间。

## 启动

```bash
docker compose up --build -d
docker compose ps          # 等待 healthy
curl -s http://localhost:8000/healthz
```

OpenAPI 文档：<http://localhost:8000/docs>

镜像以非 root 用户运行，容器只读挂载根文件系统（`read_only: true`、
`no-new-privileges`），并带 `HEALTHCHECK`。

### 不用 Docker 时

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API

### `GET /healthz`

```json
{"status": "ok", "version": "1.0.0"}
```

### `POST /plan`

请求体：

```json
{
  "zones": [
    {
      "name": "a.example.com",
      "records":     [{"name": "www.a.example.com", "type": "CNAME", "value": "svc.b.example.com", "ttl": 60}],
      "new_records": [{"name": "www.a.example.com", "type": "CNAME", "value": "svc2.b.example.com", "ttl": 60}]
    },
    {
      "name": "b.example.com",
      "records":     [{"name": "svc.b.example.com",  "type": "A", "value": "10.20.1.10"}],
      "new_records": [{"name": "svc.b.example.com",  "type": "A", "value": "10.20.1.10"},
                      {"name": "svc2.b.example.com", "type": "A", "value": "10.20.2.10"}]
    }
  ],
  "required_domains": ["www.a.example.com"]
}
```

`records` 是现状，`new_records` 是该区切换后的状态；两者相同表示“这个区不变，
但仍参与解析”。

响应 `status` 有三种：

| status | 含义 |
| --- | --- |
| `safe_order_found` | 存在安全顺序，`order` / `steps` 给出逐步操作与前后解析结果 |
| `already_broken` | 现状就解析不到 IPv4，`fault_chains` 给出故障链 |
| `no_safe_order` | 无完整安全顺序，`safe_prefix` + `blocked_zones` 给出卡点 |

每条失败链都带：

* `category`：`missing_record` / `cname_loop` / `external_target` / `chain_too_long`
* `category_label`：`缺记录` / `成环` / `链出已提交的权威区` / `CNAME 链过长`
* `detail`：缺记录时进一步区分 `nodata`（名称存在但无 A/CNAME）与
  `nxdomain`（名称不存在）
* `chain`：可读链路，例如
  `www.a.example.com --CNAME--> u.b.example.com => 无记录（NXDOMAIN）`
* `trace`：结构化的逐跳记录，便于值班员复制进工单

## 调用示例

仓库自带 5 个场景，可直接用 curl 调用：

```bash
# 1) 存在安全顺序：必须先发 b，再发 a
curl -s -X POST http://localhost:8000/plan \
  -H 'Content-Type: application/json' \
  --data-binary @examples/01-safe-order.json | python -m json.tool

# 2) 现状即故障：CNAME 指向一个没有记录的区
curl -s -X POST http://localhost:8000/plan \
  -H 'Content-Type: application/json' \
  --data-binary @examples/02-already-broken.json | python -m json.tool

# 3) 无安全顺序：a/b 互为前置，c 可安全先发
curl -s -X POST http://localhost:8000/plan \
  -H 'Content-Type: application/json' \
  --data-binary @examples/03-no-safe-order.json | python -m json.tool

# 4) 无安全顺序且卡点同时含“缺记录 + 成环”
curl -s -X POST http://localhost:8000/plan \
  -H 'Content-Type: application/json' \
  --data-binary @examples/04-blocked-mixed.json | python -m json.tool

# 5) 冷缓存回源估算：3 个区（RTT 分别 8/25/12 ms）、11 条带时间戳的 A 查询
curl -s -X POST http://localhost:8000/simulate \
  -H 'Content-Type: application/json' \
  --data-binary @examples/05-cache-restart.json | python -m json.tool
```

场景 1 的响应节选：

```json
{
  "status": "safe_order_found",
  "order": ["b.example.com", "a.example.com"],
  "steps": [
    {"step": 1, "zone": "b.example.com",
     "addresses_before": {"www.a.example.com": ["10.20.1.10"]},
     "addresses_after":  {"www.a.example.com": ["10.20.1.10"]},
     "all_required_resolvable": true},
    {"step": 2, "zone": "a.example.com",
     "addresses_before": {"www.a.example.com": ["10.20.1.10"]},
     "addresses_after":  {"www.a.example.com": ["10.20.2.10"]},
     "all_required_resolvable": true}
  ]
}
```

场景 4 的响应节选：

```json
{
  "status": "no_safe_order",
  "safe_prefix": ["d.example.com"],
  "blocked_zones": [
    {"zone": "a.example.com",
     "categories": [{"category": "missing_record", "label": "缺记录"}],
     "failures": [{"chain": "one.a.example.com --CNAME--> gone.b.example.com => 无记录（NXDOMAIN）"}]},
    {"zone": "c.example.com",
     "categories": [{"category": "cname_loop", "label": "成环"}],
     "failures": [{"chain": "two.a.example.com --CNAME--> keep.c.example.com --CNAME--> keep.c.example.com => 回到 keep.c.example.com，成环"}]}
  ]
}
```

也可以不启动服务、直接跑同一套逻辑：

```bash
python examples/demo.py
```

## `POST /simulate`

请求体（`zones` 与 `/plan` 相同，另加 `latency_ms`；`queries` 最多 500 条）：

```json
{
  "zones": [
    {
      "name": "example.com",
      "latency_ms": 8,
      "records":     [{"name": "www.example.com", "type": "CNAME", "value": "edge.cdn.example.net", "ttl": 30}],
      "new_records": [{"name": "www.example.com", "type": "CNAME", "value": "edge.cdn.example.net", "ttl": 30}]
    },
    {
      "name": "cdn.example.net",
      "latency_ms": 25,
      "records":     [{"name": "edge.cdn.example.net", "type": "A", "value": "10.10.0.10", "ttl": 60},
                      {"name": "edge.cdn.example.net", "type": "A", "value": "10.10.0.11", "ttl": 45}],
      "new_records": [{"name": "edge.cdn.example.net", "type": "A", "value": "10.10.0.10", "ttl": 60},
                      {"name": "edge.cdn.example.net", "type": "A", "value": "10.10.0.11", "ttl": 45}]
    }
  ],
  "queries": [
    {"name": "www.example.com", "at_ms": 0},
    {"name": "www.example.com", "at_ms": 5},
    {"name": "www.example.com", "at_ms": 20000}
  ]
}
```

响应结构：

| 字段 | 含义 |
| --- | --- |
| `zones[].upstream_queries` | 该区收到的回源查询总数 |
| `zones[].seconds` | 仅列出有回源的秒：`[{"second": 0, "queries": 2}, ...]` |
| `zones[].peak` | 该区回源最多的秒（并列取最早）；从未回源为 `null` / `0` |
| `total_upstream_queries` | 所有区回源次数之和 |
| `finish_ms` | 最后一条查询完成的时刻 |
| `results[]` | 每条查询：`domain`、`started_at_ms`、`completion_ms`、`resolved`、`addresses`、失败时的 `category` / `category_label` / `detail` / `message` / `chain` / `trace`，以及 `cache_hits` / `coalesced_waits` / `upstream_sent` |
| `failure_summary` | 按失败类别统计的查询数 |

场景 5 的响应节选：

```json
{
  "mode": "cold_cache_simulation",
  "zones": [
    {"zone": "example.com", "latency_ms": 8, "upstream_queries": 5,
     "seconds": [{"second": 0, "queries": 2}, {"second": 15, "queries": 1},
                 {"second": 20, "queries": 1}, {"second": 40, "queries": 1}],
     "peak": {"second": 0, "queries": 2}},
    {"zone": "core.example.org", "latency_ms": 12, "upstream_queries": 4,
     "seconds": [{"second": 0, "queries": 1}, {"second": 15, "queries": 1},
                 {"second": 40, "queries": 2}],
     "peak": {"second": 40, "queries": 2}}
  ],
  "total_upstream_queries": 12,
  "finish_ms": 40212,
  "results": [
    {"query_id": 0, "domain": "www.example.com", "started_at_ms": 0,
     "completion_ms": 33, "resolved": true,
     "addresses": ["10.10.0.10", "10.10.0.11"],
     "chain": "www.example.com --CNAME--> edge.cdn.example.net => A 10.10.0.10, 10.10.0.11",
     "cache_hits": 0, "coalesced_waits": 0, "upstream_sent": 2},
    {"query_id": 10, "domain": "gone.core.example.org", "started_at_ms": 40200,
     "completion_ms": 40212, "resolved": false, "addresses": [],
     "category": "missing_record", "category_label": "缺记录", "detail": "nxdomain",
     "chain": "gone.core.example.org => 无记录（NXDOMAIN）",
     "cache_hits": 0, "coalesced_waits": 0, "upstream_sent": 1}
  ]
}
```

手工核对要点：`at_ms=0` 与 `at_ms=5` 的两条 `www.example.com` 在第一条回包
（8ms）前都未命中，第二次查找加入在途请求，因此 `example.com` 第 0 秒只有
2 次回源（`www` + `api` 各一次别名查询），`cdn.example.net` 只回源 1 次。
`www` 别名 TTL 30 从 8ms 回包起算，到 38ms 失效，所以第二条 `www` 查询在
`40s` 需要重新取别名与地址；`api` 的地址 TTL 15 从 20ms 起算，到 35ms 失效，
所以它在 `15s` 时同样回源一次。

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```

覆盖：安全顺序、必须后发/先发的依赖、现状故障（缺记录 / 成环 / 链出区 /
链过长 / NODATA vs NXDOMAIN）、无解时的最大前缀与逐区卡点、随机小规模实例与
穷举结果的等价性校验、请求校验与 HTTP 状态码。

`/simulate` 另有 40 余项测试：回包对新查找可见、TTL 从收包起算与最短 TTL、
到期边界、缺记录不缓存、TTL 0、同毫秒并发合流、CNAME 链各查询独立续解、
按发出时刻归秒与并列峰值取最早、500 条查询上限与性能，以及与 `/plan`
解析器在随机链路上的失败类别逐条等价。

## 交付状态

**已实现**

* `POST /simulate`：`/plan` 格式的区 + 每区正整数 `latency_ms` + 最多 500 条
  `{name, at_ms}` A 查询；请求校验复用 `/plan` 的区/记录规则（非嵌套、归属、
  A/CNAME 冲突、区顶点 CNAME、IPv4 等），额外校验耗时为严格正整数。
* 事件驱动的确定性模拟（`app/simulator.py`）：固定往返耗时、同毫秒内
  “过期 → 回包 → 新查找”、按名缓存、最短 TTL 且从收包起算、缺记录不缓存、
  同名在途回包合流、各查询独立续解 CNAME 链。
* 按发出时刻统计每区回源次数与秒分布，给出每区最早峰值秒，并逐条返回 IPv4
  地址或与 `/plan` 完全一致的失败类别 / 文案 / 链路。
* 失败判定逻辑从 `app/resolver.py` 抽成共享构造函数，`/plan` 行为不变
  （逐字核对过故障消息）；`/simulate` 与 `/plan` 在随机链路上等价。
* Docker Compose 与镜像无需改动即可提供新接口；新增示例
  `examples/05-cache-restart.json` 与 `examples/demo.py` 的模拟输出。

**已验证**

* `pytest`：75 项全部通过（原有 37 项 + 新增 38 项），`mypy app` 无报错。
* 手工推导场景与断言一致：TTL 起算点、恰好到期、同毫秒可见、在途合流、
  500 条上限下的逐秒回源分布、CNAME 链 15+25+5=45ms。
* 独立实现的**逐毫秒朴素重放**（不复用模拟器代码）与事件模拟在示例场景及
  60 组随机场景（多区 CNAME、TTL 0/5/10/20/100、外部目标、缺记录、不同
  RTT）上逐条比对：完成时刻、失败类别、缓存命中、合流次数、每区逐秒计数
  全部一致。
* 用 uvicorn 实际启动服务，`curl /healthz` 与 `curl -X POST /simulate
  --data-binary @examples/05-cache-restart.json` 返回预期 JSON；
  非法请求返回 422。

**未完成 / 未验证**

* **未在真实 Docker 中构建运行**：当前环境没有 Docker 守护进程，Dockerfile
  与 `docker-compose.yml` 未改动、仅做了 YAML 解析校验；镜像内容只需 `app/`
  （新模块 `app/simulator.py` 随 `COPY app ./app` 进入镜像）。请在有权访问
  镜像仓库的机器上执行 `docker compose up --build -d` 后再用上面的 curl 复核。
* 未做真实递归解析器（BIND/Unbound 等）的对照压测；`latency_ms` 固定往返、
  无排队/丢包/重传，负应答不缓存，因此结果是偏保守的估算而非实测流量。
* 未实现负缓存、EDNS、并发连接数限制、权威区 NS 委派选择和 TCP 重试等
  真实解析器行为。


## 限制

* 只支持 A / CNAME；AAAA、SRV、通配符等不参与推演。
* 最多 8 个区、每区 256 条记录、最多 64 个必保域名、最多 500 条模拟查询。
* 同一名称的多条 A 记录视为一个 RRset（多个地址任选其一即视为可解析）。
* 不校验外部区真实状态；链出已提交区一律按“无法确认”处理。
* `/simulate` 的回源耗时是固定的往返毫秒数，不含排队、丢包与重传；
  观测窗口内区数据不变，不使用 `new_records`。
