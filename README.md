# 跨区 DNS 变更发布顺序规划器

给内网 DNS 值班员用的 FastAPI 服务。提交最多 8 个**互不嵌套**的权威区的
新旧 A / CNAME 记录，以及必须持续可解析的业务域名，服务按**无缓存权威记录**
推演每一次**整区切换**后的解析状态，回答三件事：

1. **有没有安全顺序**：有则返回逐步发布顺序，并给出每一步前后的解析结果。
2. **现状是否已经坏了**：若按现状（所有区都还是旧记录）就查不到 IPv4 地址，
   直接返回故障链路，不再谈发布顺序。
3. **没有安全顺序时卡在哪**：返回能安全走到的**最大前缀**，并逐个说明剩余
   每个区若紧接着发布会断在哪条解析链上，区分**缺记录**与**成环**。

## 推演模型

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

仓库自带 4 个场景，可直接用 curl 调用：

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

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```

覆盖：安全顺序、必须后发/先发的依赖、现状故障（缺记录 / 成环 / 链出区 /
链过长 / NODATA vs NXDOMAIN）、无解时的最大前缀与逐区卡点、随机小规模实例与
穷举结果的等价性校验、请求校验与 HTTP 状态码。

## 限制

* 只支持 A / CNAME；AAAA、SRV、通配符等不参与推演。
* 最多 8 个区、每区 256 条记录、最多 64 个必保域名。
* 同一名称的多条 A 记录视为一个 RRset（多个地址任选其一即视为可解析）。
* 不校验外部区真实状态；链出已提交区一律按“无法确认”处理。
