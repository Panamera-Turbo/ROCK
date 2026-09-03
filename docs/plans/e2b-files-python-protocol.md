# E2B Python SDK `sandbox.files` 协议调研与 ROCK 最小兼容面

> 调研快照：2026-09-01  
> 范围：E2B Python SDK 的 `files.make_dir`、`write`、`write_files`、`read(format="text"|"bytes")`、`list`、`get_info`；追踪公开参数到 envd 数据面 HTTP 协议、返回模型和异常映射。  
> 不在范围：`remove`、`rename`、`exists`、`watch_dir`、`read(format="stream")` 的完整实现，以及业务代码修改。

## 1. 结论先行

### 1.1 最小接口不是一个统一 REST 文件 API，而是 5 个 method/path 对

为满足题设调用，ROCK 数据面最少需要实现下列 **5 个 method/path 对（4 个唯一 URL path）**：

| E2B Python 调用 | 实际数据面请求 | 协议 | 本期 |
|---|---|---|---|
| `files.make_dir(...)` | `POST /filesystem.Filesystem/MakeDir` | Connect unary + ProtoJSON | 必须 |
| `files.write(...)` / `files.write_files(...)` | `POST /files` | 普通 HTTP 文件上传 | 必须 |
| `files.read(..., format="text"|"bytes")` | `GET /files` | 普通 HTTP 文件下载 | 必须 |
| `files.list(...)` | `POST /filesystem.Filesystem/ListDir` | Connect unary + ProtoJSON | 必须 |
| `files.get_info(...)` | `POST /filesystem.Filesystem/Stat` | Connect unary + ProtoJSON | 必须 |
| 上述 RPC/文件请求发生连接级故障后的健康判断 | `GET /health` | 普通 HTTP | 建议同时实现，不计入 happy-path 5 对 |

没有单独的 `write_files` 服务端接口：默认模式下一次 `write_files([...])` 会把多项编码为同一个 `POST /files` multipart 请求；`write(path, data)` 只是客户端把单项包装成 `write_files([{"path": path, "data": data}])`。[当前 Python sync 实现](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/sandbox_sync/filesystem/filesystem.py#L256-L304) [E2B 官方读写文档](https://docs.e2b.dev/filesystem/read-write)

### 1.2 三个 filesystem RPC 是 unary Connect，绝不能照搬 `commands.run()` 的流式 framing

`MakeDir`、`ListDir`、`Stat` 都是 **unary RPC**：

- `Content-Type: application/json`；
- request/response body 是裸 ProtoJSON；
- **没有** `application/connect+json`；
- **没有** `1-byte flags + 4-byte big-endian length` 的 5-byte envelope；
- 成功为 HTTP 200 + 裸 JSON，错误为非 200 + Connect error JSON。

Connect 官方协议明确区分：unary 是 `application/json` + bare message，而 streaming 才是 `application/connect+json` + 5-byte envelope。[Connect protocol：unary vs streaming](https://connectrpc.com/docs/protocol/#summary) [Unary request](https://connectrpc.com/docs/protocol/#unary-request) E2B 官方公开 API 也把这三个 path 声明为 `POST`、`application/json`。[MakeDir API](https://docs.e2b.dev/api-reference/filesystem/makedir) [ListDir API](https://docs.e2b.dev/api-reference/filesystem/listdir) [Stat API](https://docs.e2b.dev/api-reference/filesystem/stat)

### 1.3 `envdVersion` 是可选择的能力声明，不应把当前 `0.3.0` 当成默认边界

ROCK 当前代码确实锁定 [`e2b==2.34.0`](../../pyproject.toml#L115)，并通过 [`E2B_ENVD_VERSION = "0.3.0"`](../../rock/common/constants.py#L18) 在 create/connect/info 响应中声明 envd 版本，例如 [`sandbox_info`](../../rock/admin/service/e2b_sandbox_info.py#L23) 和 [`create`](../../rock/admin/entrypoints/e2b_api.py#L88)。但这是现状，不是本次 files 兼容的固定目标；实现可以提高声明，只要同时兑现被 SDK 打开的能力。

2.34.0 与 2.46.0 使用相同的三个 files feature gates：[2.46.0 gates](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/envd/versions.py#L1-L10) [2.34.0 gates](https://github.com/e2b-dev/E2B/blob/43db96a0ef2e555b96eee1a52856013fbf0dc644/packages/python-sdk/e2b/envd/versions.py#L1-L10)

| 上报的 envdVersion | `user=None` | `str` / `bytes` 默认写 | `IO` 默认写 | `gzip=True` / `use_octet_stream=True` | 非空 `metadata` |
|---|---|---|---|---|---|
| `<0.4.0` | SDK 显式使用 `user`：REST `?username=user`，RPC Basic `user:` | multipart | multipart | `<0.5.7` 时静默回退未压缩 multipart | `<0.6.2` 时客户端抛 `TemplateException`，不发请求 |
| `>=0.4.0,<0.5.7` | SDK 省略 username/Basic；服务端必须自行选择 default user | multipart | multipart | 静默回退未压缩 multipart | 客户端拒绝 |
| `>=0.5.7,<0.6.2` | 服务端推断 default user | multipart | **自动改走 octet-stream** | octet-stream；`gzip=True` 还带 `Content-Encoding:gzip` | 客户端拒绝 |
| `>=0.6.2` | 服务端推断 default user | multipart | **自动改走 octet-stream** | octet-stream / gzip | `X-Metadata-*` 到达服务端，必须校验、持久化并回读 |
| 当前官方 envd `0.7.0` | 与 `>=0.6.2` 相同 | multipart | octet-stream | octet-stream / gzip | 完整 metadata；有效 symlink返回目标 type + `symlinkTarget` |

当前官方 envd `main` 源码版本是 [`0.7.0`](https://github.com/e2b-dev/infra/blob/aed0ebdc4d0df05812215d14a09939bf8f867de7/packages/envd/pkg/version.go#L1-L3)，最新单独发布的 envd tag 是 [`0.6.13`](https://github.com/e2b-dev/infra/releases/tag/envd-v0.6.13)；两者对题设方法没有 feature-gate 差异。`0.6.2` 之后不增加新的 method/path，只沿用完整上传与当前 `EntryInfo/FileType` 语义。

**files 范围的推荐声明是 `0.6.2`**：这是让题设方法的所有公开上传选项（IO 自动 streaming、显式 octet-stream、gzip upload、metadata）都真实可用的最小版本。若本 PR 只做默认参数和基础 multipart，可阶段性声明 `0.4.0`，但不能声称 metadata 或 octet-stream 已兼容。是否能在 ROCK 全局响应中直接报 `0.6.2`，还需审计 §7.4 所述的非 files 全局 gate。

### 1.4 Python SDK 没有名为 `FileInfo` 的公开模型

题设所说的“FileInfo”在当前 Python SDK 中实际叫：

- `EntryInfo`：`list` / `get_info` 的返回元素；
- `WriteInfo`：`write` / `write_files` 的返回元素；
- `FileType`：`EntryInfo.type` / `WriteInfo.type` 的 SDK enum；
- `WriteEntry`：`write_files` 入参的 `TypedDict`。

模型定义见官方源码。[FileType / WriteInfo / EntryInfo / WriteEntry](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/sandbox/filesystem/filesystem.py#L19-L155) 官方文档的 Python 示例也显示 `get_info()` 返回 `EntryInfo`。[Filesystem info](https://docs.e2b.dev/filesystem/info)

## 2. 版本基线与兼容下限

### 2.1 当前稳定版

截至调研日：

- PyPI 最新可安装稳定版是 [`e2b==2.46.0`](https://pypi.org/project/e2b/2.46.0/)，2026-08-25 发布；
- 对应官方 release commit 是 [`d42686d9`](https://github.com/e2b-dev/E2B/tree/d42686d982f741b01f2c71da304e63846b34706f)，manifest 明确写 `version = "2.46.0"`。[manifest](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/pyproject.toml#L1-L20)
- 官方仓库 `main` 已把 manifest 推进到 `2.46.1`，但调研时 PyPI 尚未发布它；因此实现验收应以可安装的 2.46.0 为“当前最新版”，而非移动中的 `main`。

### 2.2 ROCK 兼容下限

ROCK 测试依赖固定为 `e2b==2.34.0`，故本期必须同时覆盖：

- 下限：Python SDK 2.34.0，对应 release commit [`43db96a0`](https://github.com/e2b-dev/E2B/tree/43db96a0ef2e555b96eee1a52856013fbf0dc644)；
- 上限基线：Python SDK 2.46.0，对应 release commit `d42686d9`。

### 2.3 2.34.0 与 2.46.0 的 wire 对比

| 项目 | 2.34.0 | 2.46.0 | ROCK 兼容要求 |
|---|---|---|---|
| 5 个 method/path | 与 §1.1 完全相同 | 与 §1.1 完全相同 | 路由无需按版本分叉 |
| unary body | 裸 ProtoJSON | 裸 ProtoJSON | 统一按 JSON 解析；禁止 envelope |
| unary content type | `application/json` | `application/json` | 必须接受/返回该类型 |
| unary transport | vendored `e2b_connect` | 官方 `connectrpc` + `protobuf-py` | 不依赖 Python transport 类，只依赖 wire |
| unary timeout | 主要是客户端本地 timeout，通常不发 `Connect-Timeout-Ms` | 默认有效 timeout 会发 `Connect-Timeout-Ms` | header 必须可选；有则接受，无则也能工作 |
| SDK `FileType` enum | `FILE`, `DIR` | `FILE`, `DIR`, `SYMLINK` | 当前官方有效 symlink仍返回目标的 FILE/DIR并另填 `symlinkTarget`，两版稳定兼容 |
| `/files` 默认上传 | multipart | multipart | 相同 |

2.34.0 生成客户端固定了 `Stat/MakeDir/ListDir` URL 并调用 unary。[2.34 generated client](https://github.com/e2b-dev/E2B/blob/43db96a0ef2e555b96eee1a52856013fbf0dc644/packages/python-sdk/e2b/envd/filesystem/filesystem_connect.py#L9-L58) 它的 vendored transport 直接把 JSON 放进 HTTP body，并设置 `connect-protocol-version: 1`、`content-type: application/json`，没有 envelope。[2.34 unary encoder](https://github.com/e2b-dev/E2B/blob/43db96a0ef2e555b96eee1a52856013fbf0dc644/packages/python-sdk/e2b_connect/client.py#L223-L285)

2.46.0 改用官方 `connectrpc`，但 E2B 显式选择 JSON codec以保持原 wire。[E2B JSON codec](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/envd/client_shared.py#L29-L60) Connect 官方 Python 实现仍为 unary 请求设置 `application/json` 和可选 `connect-timeout-ms`，再直接发送 codec 编码后的 body。[connect-py v0.11.1 headers](https://github.com/connectrpc/connect-py/blob/v0.11.1/src/connectrpc/_protocol_connect.py#L178-L227) [connect-py v0.11.1 unary send](https://github.com/connectrpc/connect-py/blob/v0.11.1/src/connectrpc/_client_sync.py#L292-L327)

### 2.4 当前官方 envd 基线

本次以 [`e2b-dev/infra@aed0ebdc`](https://github.com/e2b-dev/infra/tree/aed0ebdc4d0df05812215d14a09939bf8f867de7) 为服务端一手基线；其 `pkg.Version` 是 [`0.7.0`](https://github.com/e2b-dev/infra/blob/aed0ebdc4d0df05812215d14a09939bf8f867de7/packages/envd/pkg/version.go#L1-L3)。它同时实现：

- `/files` multipart和octet-stream、gzip request解压、download gzip协商；
- `X-Metadata-*` 的替换/清空及 RPC metadata回读；
- `FILE_TYPE_FILE` / `DIRECTORY` 的完整 EntryInfo；有效 symlink使用目标 type并另填 `symlinkTarget`；
- MakeDir/ListDir/Stat 的当前 Connect unary语义。

服务端证据分别见 [upload](https://github.com/e2b-dev/infra/blob/aed0ebdc4d0df05812215d14a09939bf8f867de7/packages/envd/internal/api/upload.go#L239-L425)、[download](https://github.com/e2b-dev/infra/blob/aed0ebdc4d0df05812215d14a09939bf8f867de7/packages/envd/internal/api/download.go#L19-L172)、[entry mapping](https://github.com/e2b-dev/infra/blob/aed0ebdc4d0df05812215d14a09939bf8f867de7/packages/envd/internal/services/filesystem/utils.go#L43-L67)。ROCK 不必为本题直接宣称 0.7.0，但 0.7.0 是判断“当前官方行为”的参照。

## 3. 公共 URL、routing 与鉴权

SDK 对 envd HTTP 和 RPC 共用同一个 base URL。设置 `E2B_SANDBOX_URL` 时直接使用该 URL；否则托管域名通常使用 `https://sandbox.<domain>` 并靠 headers 路由。当前连接计算逻辑见官方源码。[URL calculation](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/connection_config.py#L274-L310)

所有 5 个请求都会带这些 sandbox 级 headers：

| Header | 来源 / 语义 | 最小处理 |
|---|---|---|
| `E2b-Sandbox-Id: <id>` | shared sandbox host 的实例路由 | 必须按 id 路由并校验目标存在 |
| `E2b-Sandbox-Port: 49983` | envd 逻辑端口 | 网关若已终止该路由，可只接受/忽略；不应因大小写不同失败 |
| `X-Access-Token: <envdAccessToken>` | 数据面鉴权；secure sandbox 时存在 | 依现有网关/ROCK 决策处理 |
| `User-Agent` | SDK/transport 标识 | 仅观测，不可作鉴权 |

SDK 在 create/connect 后构造上述 routing headers。[Sandbox headers construction](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/sandbox_sync/main.py#L1146-L1164) 官方公开 API 也把 sandbox id、port 和 access token列为数据面参数。[Download API](https://docs.e2b.dev/api-reference/filesystem/download-a-file) [MakeDir API](https://docs.e2b.dev/api-reference/filesystem/makedir)

`user` 的 wire 表达因接口类型不同：

| 接口 | `user` 的 wire |
|---|---|
| `GET/POST /files` | query `username=<user>` |
| 三个 unary RPC | `Authorization: Basic base64("<user>:")`，密码为空 |

官方 API 对 RPC 的 `SandboxUserAuth` 说明就是 Basic username/no password；对 `/files` 则定义了 `username` query。[Stat user auth](https://docs.e2b.dev/api-reference/filesystem/stat) [Upload parameters](https://docs.e2b.dev/api-reference/filesystem/upload-a-file-and-ensure-the-parent-directories-exist-if-the-file-exists-it-will-be-overwritten) Python 端的 Basic 编码实现见 [authentication_header](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/envd/utils.py#L44-L57)。

`user` 必须按声明版本双态兼容：`envdVersion <0.4.0` 且调用方不传 user时，SDK会显式产生默认用户 `user`；`envdVersion >=0.4.0` 时则省略 username/Basic，服务端必须从 sandbox初始化配置自行得到 default user。调用方显式传 `user="root"` 等值时，所有版本仍会发送 query/Basic；若 ROCK 首期不支持任意用户，必须明确拒绝，不能静默按 default user执行。SDK分支见 [HTTP filesystem user selection](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/sandbox_sync/filesystem/filesystem.py#L193-L200) 和 [RPC authentication_header](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/envd/utils.py#L44-L57)。

## 4. 精确接口契约

### 4.1 `files.make_dir`

公开签名（sync；async 参数一致）：

```python
make_dir(
    path: str,
    user: Optional[str] = None,
    request_timeout: Optional[float] = None,
) -> bool
```

源码与默认值见 [2.46.0 Filesystem.make_dir](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/sandbox_sync/filesystem/filesystem.py#L602-L631)。

请求：

```http
POST /filesystem.Filesystem/MakeDir
Content-Type: application/json
Connect-Protocol-Version: 1
Connect-Timeout-Ms: 60000        # 2.46 默认有效值；2.34 可缺失
Authorization: Basic dXNlcjo=   # 仅 envdVersion<0.4.0 且未显式 user，或显式 user="user"
E2b-Sandbox-Id: <sandbox-id>
E2b-Sandbox-Port: 49983

{"path":"/tmp/a/b"}
```

Proto schema只有 `path`，response 可带完整 `entry`。[Filesystem proto](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/spec/envd/filesystem/filesystem.proto#L24-L35)

返回语义：

- 创建成功：HTTP 200、`application/json`、`{"entry": {...}}`；SDK 不读取 `entry`，直接返回 `True`；
- 目录已经存在：必须返回 Connect code `already_exists`（通常 HTTP 409 + `{"code":"already_exists","message":"..."}`），SDK 将其转成 `False`；
- path 已存在但不是目录：官方 envd 返回 `invalid_argument`；
- 父目录不存在不是错误：官方语义是递归创建所有父目录。

官方 envd 的实现分别处理“已是目录 → already_exists”“是文件 → invalid_argument”“不存在 → EnsureDirs”。[envd MakeDir server](https://github.com/e2b-dev/infra/blob/aed0ebdc4d0df05812215d14a09939bf8f867de7/packages/envd/internal/services/filesystem/dir.go#L56-L97)

### 4.2 `files.write` / `files.write_files`

公开签名：

```python
write(
    path: str,
    data: str | bytes | IO,
    user: Optional[str] = None,
    request_timeout: Optional[float] = None,
    gzip: bool = False,
    use_octet_stream: Optional[bool] = None,
    metadata: Optional[dict[str, str]] = None,
) -> WriteInfo

write_files(
    files: list[WriteEntry],
    user: Optional[str] = None,
    request_timeout: Optional[float] = None,
    gzip: bool = False,
    use_octet_stream: Optional[bool] = None,
    metadata: Optional[dict[str, str]] = None,
) -> list[WriteInfo]
```

当前签名与 feature-gate 分支见 [write/write_files](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/sandbox_sync/filesystem/filesystem.py#L256-L365)。

#### 默认 multipart 路径（所有声明版本都存在）

`data` 全是 `str` / `bytes`、`gzip=False` 且 `use_octet_stream` 为 `None` 或 `False` 时，2.34.0 和 2.46.0 在任何 envdVersion 下都发送 multipart：

```http
POST /files?username=user                 # 单文件还会有 &path=<path>；>=0.4 且 user=None 时省略 username
Content-Type: multipart/form-data; boundary=<random>
E2b-Sandbox-Id: <sandbox-id>
E2b-Sandbox-Port: 49983
X-Access-Token: <token>

--<boundary>
Content-Disposition: form-data; name="file"; filename="/target/path.txt"
...
<raw file bytes>
--<boundary>--
```

具体映射：

| 调用形态 | query | multipart |
|---|---|---|
| `write(path, str_or_bytes)` | `path=<path>` + 可选 `username` | 1 个 part，name 必须是 `file`，filename 为 path |
| `write_files([one])` | 同上 | 同上 |
| `write_files([many str/bytes])` | 不带 `path`，只带可选 `username` | 每项 1 个同名 `file` part，各自 filename 是该项 path，顺序与输入一致 |
| `write_files([])` | 不发请求 | 客户端直接返回 `[]` |

SDK 组装 multipart 的源码明确使用 `("file", (file_path, file_data))`，单项时额外设置 `params["path"]`。[multipart construction](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/sandbox/filesystem/filesystem.py#L265-L278) [POST branch](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/sandbox_sync/filesystem/filesystem.py#L403-L445)

服务端必须：

1. 将 `str` 按 UTF-8 收到的 bytes写入；`bytes` 必须逐字节保真；显式 `use_octet_stream=False` 的 `IO` 仍可能产生流式/chunked multipart；
2. 覆盖既有文件；
3. 自动创建缺失的父目录；
4. 对显式 username应用相应相对路径基准和 ownership；username缺失（上报 >=0.4.0）时选择 sandbox default user；其他用户名若不在首期范围必须明确拒绝；
5. 批量响应按输入顺序返回每个文件的 `WriteInfo`；
6. 不能依赖 `Content-Length`，也不应把大 multipart 全量读入内存。

官方 upload API确认 multipart 和 octet-stream 两种 content type、`path`/`username` query、覆盖与自动创建父目录语义。[Upload API](https://docs.e2b.dev/api-reference/filesystem/upload-a-file-and-ensure-the-parent-directories-exist-if-the-file-exists-it-will-be-overwritten) 官方 envd 逐 part 处理 `name="file"`，从 query path 或 multipart filename 解析目标。[envd multipart handler](https://github.com/e2b-dev/infra/blob/aed0ebdc4d0df05812215d14a09939bf8f867de7/packages/envd/internal/api/upload.go#L159-L236) [envd request dispatch](https://github.com/e2b-dev/infra/blob/aed0ebdc4d0df05812215d14a09939bf8f867de7/packages/envd/internal/api/upload.go#L239-L359)

#### `>=0.5.7`：octet-stream、IO 自动切换与 gzip upload

一旦上报 `>=0.5.7`，下面任一条件会选择 octet-stream：

- 任一 `WriteEntry.data` 是 file-like `IO`，且调用方未显式传 `use_octet_stream=False`；
- `use_octet_stream=True`；
- `gzip=True`，即使同时显式传了 `use_octet_stream=False`。

精确选择矩阵：

| data / 参数 | `<0.5.7` | `>=0.5.7` |
|---|---|---|
| 全部 `str/bytes`，`use_octet_stream=None`，`gzip=False` | multipart | multipart |
| 任一 `IO`，`use_octet_stream=None`，`gzip=False` | multipart | **octet-stream，一项一个请求** |
| 任意 data，`use_octet_stream=True` | 回退 multipart | **octet-stream，一项一个请求** |
| 任一 `IO`，`use_octet_stream=False`，`gzip=False` | multipart | multipart |
| 任意 data，`gzip=True` | 回退未压缩 multipart | **gzip-compressed octet-stream** |

octet-stream 请求是：

```http
POST /files?path=<path>&username=<user>   # >=0.4 且 user=None 时省略 username
Content-Type: application/octet-stream
Content-Encoding: gzip       # 仅 gzip=True 时

<raw bytes or gzip stream>
```

如果 batch 中任何一项触发 octet-stream，整个 batch 都进入该分支并拆为 N 个请求；sync SDK顺序发送，async SDK用 `asyncio.gather` 并发发送全部项。服务端不能假设 `write_files` 永远是一条 multipart请求。[sync selection/octet branch](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/sandbox_sync/filesystem/filesystem.py#L333-L402) [async concurrent branch](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/sandbox_async/filesystem/filesystem.py#L362-L438)

官方当前 envd要求 raw upload必须带 `path`，在写入前解 gzip，并复用覆盖/父目录/ownership逻辑。[raw upload handler](https://github.com/e2b-dev/infra/blob/aed0ebdc4d0df05812215d14a09939bf8f867de7/packages/envd/internal/api/upload.go#L391-L425) [upload dispatch](https://github.com/e2b-dev/infra/blob/aed0ebdc4d0df05812215d14a09939bf8f867de7/packages/envd/internal/api/upload.go#L286-L340)

#### `>=0.6.2`：metadata成为必须兑现的公开参数

非空 `metadata` 不再被客户端拦截，而是同一 map以 `X-Metadata-<key>: <value>` 加到每个 multipart或 octet-stream请求。SDK先要求 key是合法 HTTP token、value是 printable US-ASCII；服务端仍必须执行官方限制：key lower-case持久化到 `user.e2b.*` xattr、单 key最多 246 bytes、每文件总 metadata最多 4096 bytes、重复 header取第一个值。[SDK validation/header mapping](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/sandbox/filesystem/filesystem.py#L339-L378) [官方 upload metadata contract](https://docs.e2b.dev/api-reference/filesystem/upload-a-file-and-ensure-the-parent-directories-exist-if-the-file-exists-it-will-be-overwritten)

覆盖文件时 metadata是**整组替换**：本次缺少的旧 key被删除；请求没有任何 `X-Metadata-*` 时清空旧 metadata。成功的 REST `WriteInfo`、后续 RPC `Stat/ListDir` 都必须回读实际持久化值。当前官方 envd实现了提取、写入和回读。[metadata extraction/write](https://github.com/e2b-dev/infra/blob/aed0ebdc4d0df05812215d14a09939bf8f867de7/packages/envd/internal/api/upload.go#L28-L52) [metadata replacement/readback](https://github.com/e2b-dev/infra/blob/aed0ebdc4d0df05812215d14a09939bf8f867de7/packages/envd/internal/api/upload.go#L135-L154)

#### 上传成功响应（两种 upload content type相同）

```http
HTTP/1.1 200 OK
Content-Type: application/json

[
  {"path":"/home/user/a.txt","name":"a.txt","type":"file"},
  {"path":"/home/user/b.bin","name":"b.bin","type":"file"}
]
```

注意这里是普通 REST JSON，`type` 必须是小写 `"file"`，不是 ProtoJSON enum `"FILE_TYPE_FILE"`。SDK 会把它映射为 `WriteInfo(type=FileType.FILE)`；未知字符串映射为 `None`。[WriteInfo.from_dict](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/sandbox/filesystem/filesystem.py#L47-L90)

对 `write()`，响应数组长度不是 1 会被 SDK 转成 `SandboxException`；`write_files()` 同样要求响应是非空 list，但不会自行验证“元素数等于输入数”，因此服务端必须主动保证一一对应。[response parsing](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/sandbox_sync/filesystem/filesystem.py#L391-L445)

### 4.3 `files.read(format="text"|"bytes")`

公开签名的相关 overload：

```python
read(
    path: str,
    format: Literal["text"] = "text",
    user: Optional[str] = None,
    request_timeout: Optional[float] = None,
    gzip: bool = False,
) -> str

read(
    path: str,
    format: Literal["bytes"],
    user: Optional[str] = None,
    request_timeout: Optional[float] = None,
    gzip: bool = False,
) -> bytearray
```

实际实现还接受 `format="stream"` 和 `stream_idle_timeout=None`，但题设不要求 stream。本次必须注意：Python 的 `"bytes"` 返回类型实际是 **`bytearray`**，不是 `bytes`。[read overloads and implementation](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/sandbox_sync/filesystem/filesystem.py#L105-L255)

`format` 完全是客户端选项，不进入 wire。两种调用都发送：

```http
GET /files?path=<url-encoded-path>&username=user  # >=0.4 且 user=None 时省略 username
Accept-Encoding: gzip        # 仅调用方 gzip=True 时显式设置；服务端可返回 identity
E2b-Sandbox-Id: <sandbox-id>
E2b-Sandbox-Port: 49983
X-Access-Token: <token>
```

成功响应是文件原始内容。SDK本地执行：

- `format="text"` → `response.text`；
- `format="bytes"` → `bytearray(response.content)`；
- HTTP 客户端会透明解压 gzip，因此服务端无论返回 identity 还是合法 gzip，最终值都应是原文件内容。

`read(gzip=True)` 不受 envdVersion gate：所有声明版本都会显式请求 gzip。为完整参数兼容，服务端应像官方 envd一样做 `Accept-Encoding` 协商并可返回 `Content-Encoding:gzip`；返回 identity仍能产生正确值，但不能因看到 gzip而错误返回 406。官方当前实现对 gzip编码后传输、identity和 Range/conditional请求做了区分。[envd download implementation](https://github.com/e2b-dev/infra/blob/aed0ebdc4d0df05812215d14a09939bf8f867de7/packages/envd/internal/api/download.go#L107-L172)

官方 download API定义了 `path`、`username`、200 binary body，以及 400/401/404/406/500/502错误。[Download API](https://docs.e2b.dev/api-reference/filesystem/download-a-file)

`request_timeout` 只控制客户端 HTTP请求，不作为 query/body 传给 `/files`；公开方法默认参数虽是 `None`，其有效默认值来自 `ConnectionConfig`，为 60 秒，传 `0` 禁用。[request timeout default](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/connection_config.py#L22-L34) [timeout resolution](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/connection_config.py#L259-L272)

### 4.4 `files.list`

公开签名：

```python
list(
    path: str,
    depth: Optional[int] = 1,
    user: Optional[str] = None,
    request_timeout: Optional[float] = None,
) -> list[EntryInfo]
```

SDK 在发请求前拒绝 `depth < 1`（`depth=None` 除外），抛 `InvalidArgumentException`。`None` 在 wire 上变成 proto 默认 0；官方 envd 将 0 解释为默认 depth 1。[SDK list](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/sandbox_sync/filesystem/filesystem.py#L447-L484) [envd ListDir depth](https://github.com/e2b-dev/infra/blob/aed0ebdc4d0df05812215d14a09939bf8f867de7/packages/envd/internal/services/filesystem/dir.go#L17-L53)

请求：

```http
POST /filesystem.Filesystem/ListDir
Content-Type: application/json
Connect-Protocol-Version: 1
Connect-Timeout-Ms: 60000      # 2.46 常见；2.34 可缺失
Authorization: Basic dXNlcjo=   # 仅 <0.4 且 user=None，或显式 user="user"

{"path":"/work","depth":2}
```

成功 ProtoJSON：

```json
{
  "entries": [
    {
      "name": "a.txt",
      "type": "FILE_TYPE_FILE",
      "path": "/work/a.txt",
      "size": "3",
      "mode": 420,
      "permissions": "-rw-r--r--",
      "owner": "user",
      "group": "user",
      "modifiedTime": "2026-09-01T00:00:00Z"
    },
    {
      "name": "sub",
      "type": "FILE_TYPE_DIRECTORY",
      "path": "/work/sub",
      "size": "0",
      "mode": 493,
      "permissions": "drwxr-xr-x",
      "owner": "user",
      "group": "user",
      "modifiedTime": "2026-09-01T00:00:00Z"
    }
  ]
}
```

字段和 enum来自官方 proto；ProtoJSON 使用 lowerCamelCase 字段名和 proto enum 名。[EntryInfo / FileType / ListDir schema](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/spec/envd/filesystem/filesystem.proto#L48-L78) 官方 API允许 int64 `size` 用 JSON string 或 integer，canonical ProtoJSON通常为 string。[ListDir API](https://docs.e2b.dev/api-reference/filesystem/listdir)

SDK 会丢弃 `type` 无法映射的 entries。因此普通文件必须返回 `FILE_TYPE_FILE`，目录必须返回 `FILE_TYPE_DIRECTORY`；若错发 REST 的 `"file"` / `"dir"`，会导致 decode 或映射错误，而不是“只是显示不同”。[entry mapping and list filter](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/sandbox/filesystem/filesystem.py#L38-L53) [list filter](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/sandbox_sync/filesystem/filesystem.py#L467-L482)

### 4.5 `files.get_info`

公开签名：

```python
get_info(
    path: str,
    user: Optional[str] = None,
    request_timeout: Optional[float] = None,
) -> EntryInfo
```

请求：

```http
POST /filesystem.Filesystem/Stat
Content-Type: application/json
Connect-Protocol-Version: 1
Authorization: Basic dXNlcjo=   # 仅 <0.4 且 user=None，或显式 user="user"

{"path":"/work/a.txt"}
```

响应为 `{"entry": <与 list 相同的完整 EntryInfo>}`。SDK 将 proto response 映射为 Python `EntryInfo`；path 不存在的 `not_found` 必须最终映射为 `FileNotFoundException`。[SDK get_info](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/sandbox_sync/filesystem/filesystem.py#L517-L543) [Stat API](https://docs.e2b.dev/api-reference/filesystem/stat)

## 5. 数据模型

### 5.1 `WriteEntry`

`WriteEntry` 是 `TypedDict`，不是 dataclass：

```python
class WriteEntry(TypedDict):
    path: str
    data: str | bytes | IO
```

两个 key 都必填。官方示例使用普通 dict列表；`str` 作为 UTF-8文本上传，`bytes` 二进制保真，file-like `IO` 可流式上传。[WriteEntry source](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/sandbox/filesystem/filesystem.py#L149-L155) [Python multi-write example](https://docs.e2b.dev/filesystem/read-write#writing-multiple-files)

### 5.2 `FileType`

2.46.0：

```python
class FileType(Enum):
    FILE = "file"
    DIR = "dir"
    SYMLINK = "symlink"
```

2.34.0 只有 `FILE`、`DIR`。[2.46 model](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/sandbox/filesystem/filesystem.py#L19-L35) [2.34 model](https://github.com/e2b-dev/E2B/blob/43db96a0ef2e555b96eee1a52856013fbf0dc644/packages/python-sdk/e2b/sandbox/filesystem/filesystem.py#L16-L35)

wire 与 Python enum 的映射必须区分：

| 来源 | wire `type` | Python |
|---|---|---|
| REST `/files` upload response | `"file"` | `FileType.FILE` |
| RPC regular file | `"FILE_TYPE_FILE"` | `FileType.FILE` |
| RPC directory | `"FILE_TYPE_DIRECTORY"` | `FileType.DIR` |
| RPC schema允许直接表示 symlink（2.46） | `"FILE_TYPE_SYMLINK"` | `FileType.SYMLINK`；但当前官方 envd对有效 symlink不这样编码 |
| RPC unspecified/unknown | `"FILE_TYPE_UNSPECIFIED"` 或未知 | `None`；`list` 会过滤该 entry |

若要同时兼容 2.34 与 2.46，应复现当前官方 envd：对有效 symlink返回目标的 `FILE_TYPE_FILE` / `FILE_TYPE_DIRECTORY`，并同时填写 `symlinkTarget`；不要直接返回 `FILE_TYPE_SYMLINK`。这样 2.34仍得到有效 FileType，2.46也能识别链接。官方 shared entry逻辑先 `Lstat` 获取链接、再 `Stat` 目标决定 type；官方测试明确断言 symlink-to-file是 `FILE_TYPE_FILE`。[官方 entry构造](https://github.com/e2b-dev/infra/blob/aed0ebdc4d0df05812215d14a09939bf8f867de7/packages/shared/pkg/filesystem/entry.go#L43-L95) [官方 stat symlink测试](https://github.com/e2b-dev/infra/blob/aed0ebdc4d0df05812215d14a09939bf8f867de7/packages/envd/internal/services/filesystem/stat_test.go#L54-L89)

### 5.3 `WriteInfo`

```python
@dataclass
class WriteInfo:
    name: str
    type: Optional[FileType]
    path: str
    metadata: Optional[dict[str, str]] = None  # keyword-only
```

它只用于普通 `/files` upload response，不要求 `size/mode/owner`。[WriteInfo source](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/sandbox/filesystem/filesystem.py#L56-L90)

### 5.4 `EntryInfo`（题设所谓 FileInfo）

```python
@dataclass
class EntryInfo(WriteInfo):
    size: int
    mode: int
    permissions: str
    owner: str
    group: str
    modified_time: datetime
    symlink_target: Optional[str] = None
    # inherited: name, type, path, metadata
```

服务端最少要准确返回 `name/type/path/size/mode/permissions/owner/group/modifiedTime`；symlink时还应返回 `symlinkTarget`，metadata可省略。当前 SDK完整映射见 [EntryInfo source](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/sandbox/filesystem/filesystem.py#L93-L146)，官方文档给出了文件和目录示例。[Filesystem info](https://docs.e2b.dev/filesystem/info)

## 6. 错误响应与 Python 异常映射

### 6.1 普通 `/files` HTTP

错误 body应为：

```json
{"code": 404, "message": "path '/x' does not exist"}
```

当前 Python SDK映射：

| HTTP | Python结果 |
|---:|---|
| 400 | `InvalidArgumentException` |
| 401 | `AuthenticationException` |
| 404 | `FileNotFoundException`（filesystem override） |
| 429 | `RateLimitException` |
| 502 | sandbox timeout/terminated 语义的 `TimeoutException` |
| 507 | `NotEnoughSpaceException` |
| 其他非 2xx（包括 406、500） | `SandboxException(f"<status>: <message>")` |

映射来源见 [envd HTTP error map](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/envd/api.py#L21-L30) 和 [filesystem 404 override](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/sandbox_sync/filesystem/filesystem.py#L57-L73)。官方 `/files` OpenAPI列出的错误集见 [download](https://docs.e2b.dev/api-reference/filesystem/download-a-file) 和 [upload](https://docs.e2b.dev/api-reference/filesystem/upload-a-file-and-ensure-the-parent-directories-exist-if-the-file-exists-it-will-be-overwritten)。

### 6.2 unary Connect RPC

成功必须是 HTTP 200 + `application/json` + 对应 response ProtoJSON。错误必须是非 200 + `application/json` Connect error，例如：

```http
HTTP/1.1 409 Conflict
Content-Type: application/json

{"code":"already_exists","message":"directory already exists: /x"}
```

Connect 规范要求 unary error使用非 200 和 JSON error body。[Unary response/errors](https://connectrpc.com/docs/protocol/#unary-response)

SDK映射：

| Connect code | Python结果 |
|---|---|
| `already_exists`（仅 `make_dir` 特判） | `False` |
| `not_found` | `FileNotFoundException` |
| `invalid_argument` | `InvalidArgumentException` |
| `unauthenticated` | `AuthenticationException` |
| `resource_exhausted` | `RateLimitException` |
| `unavailable` | `TimeoutException` |
| `canceled` / `deadline_exceeded` | `TimeoutException` |
| 其他 | `SandboxException` |

当前映射见 [RPC default map](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/envd/rpc.py#L18-L32) 和 [filesystem override/make_dir special case](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/sandbox_sync/filesystem/filesystem.py#L57-L69)。

### 6.3 `/health` 为什么建议一起实现

发生 RPC transport failure 或 `/files` 的 `RemoteProtocolError` 时，SDK会 `GET /health`，用它区分 sandbox 已退出和暂时网络故障；health 返回 502会被视作 sandbox不再运行，2xx视作仍运行。[health probe](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/envd/api.py#L33-L62) [RPC health-assisted mapping](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/envd/rpc.py#L118-L148)

所以 `/health` 不影响 happy path，但若缺失，部分故障只会泄露底层 transport exception，无法得到 E2B 预期的 `TimeoutException`。

## 7. 版本分层能力矩阵与建议声明

### 7.1 不论上报哪个版本，五个 method/path 的共同必选语义

| 能力 | 所有层都必须保证 |
|---|---|
| make dir | `path` 绝对/相对路径；递归父目录；重复创建返回 `False`；有/无 `Connect-Timeout-Ms` |
| single write | `str` UTF-8、`bytes` 保真；覆盖；父目录自动创建；multipart；流式 body不要求 Content-Length |
| batch write | 空列表不请求；单项/多项；输入与响应顺序；不同目标目录；部分失败语义明确 |
| read | `format="text"` 返回 str；`format="bytes"` 返回 bytearray；原始内容保真；`gzip=True` 可协商 gzip或identity |
| list | 默认 `depth=1`、`depth=2`；完整 EntryInfo；REST/RPC enum不可混用 |
| get info | 文件和目录完整 EntryInfo；不存在映射 `FileNotFoundException` |
| common | routing/token；2.34/2.46 双版本；unary裸 ProtoJSON；正确 default/explicit user路径和ownership语义 |

### 7.2 版本层级对公开参数的实际影响

| 公开参数/行为 | `<0.4.0` | `>=0.4.0,<0.5.7` | `>=0.5.7,<0.6.2` | `>=0.6.2` / 官方 0.7.0 |
|---|---|---|---|---|
| 所有方法 `user=None` | SDK发送默认 `user` | SDK省略；服务端推断 | 同左 | 同左 |
| 显式 `user="root"` 等 | query或Basic发送；服务端执行该用户语义 | 相同 | 相同 | 相同 |
| write `str/bytes` 默认 | multipart | multipart | multipart | multipart |
| write `IO`，`use_octet_stream=None` | multipart | multipart | octet-stream | octet-stream |
| `use_octet_stream=True` | 静默回退 multipart | 静默回退 multipart | octet-stream | octet-stream |
| `use_octet_stream=False`、`gzip=False` | multipart | multipart | multipart（即使 data是IO） | multipart |
| upload `gzip=True` | 静默回退未压缩 multipart | 同左 | gzip octet-stream | gzip octet-stream |
| 非空 `metadata` | 客户端 `TemplateException` | 同左 | 同左 | `X-Metadata-*`；服务端必须完整支持 |
| read `gzip=True` | 不受 gate，发 `Accept-Encoding:gzip` | 相同 | 相同 | 相同 |
| list/get_info metadata | 可省略 | 可省略 | 可省略 | 写入后必须从 RPC EntryInfo回读 |
| 有效 symlink | 目标 FILE/DIR + `symlinkTarget` | 相同 | 相同 | 相同；这是官方 0.7的跨 2.34/2.46稳态 |

### 7.3 建议的可声明版本

| 建议 profile | 可声明版本 | 前置实现 | 适用结论 |
|---|---:|---|---|
| 基础兼容 | `0.4.0` | 五个 method/path + multipart + 服务端 default user | 能覆盖题设方法的默认 `str/bytes` 调用及 file-like multipart，但不提供 metadata/octet-stream公开能力 |
| 流式上传层 | `0.5.7` | 再加 raw octet-stream、gzip解压、N请求 batch、并发与限流 | IO在默认参数下即会自动触发；只应在相关 handler完成后声明 |
| **files 全参数（推荐）** | **`0.6.2`** | 再加 metadata验证、xattr/等价持久化、替换/清空、REST/RPC回读 | 是完整支持题设方法所有公开参数的最小声明；没有必要仅为本题冒进到 0.7.0 |
| 当前官方 envd parity | `0.7.0` | `0.6.2`能力 + 目标 type/`symlinkTarget`和全局其他能力 | 仅在做完整 envd兼容时声明 |

因此，若“兼容 files 方法”包含其公开可选参数，本设计应以 **`envdVersion=0.6.2`** 为目标并实现全部三层 wire；若本期只验收默认文本/二进制写和读/list/info，可先报 `0.4.0`。不建议停在 `0.5.7`：它已经引入最复杂的 N请求/并发上传，却仍让 metadata不可用。

### 7.4 `envdVersion` 是全局声明，最终值不能只看 files

files 范围内 `0.6.2` 是最小全参数版本，但 E2B SDK还用同一个值 gate其他模块。例如 `ENVD_ENVD_CLOSE=0.5.2`，上报 0.5.7/0.6.2 后 `commands.close_stdin()` 不再客户端拒绝，而会真正请求 `/process.Process/CloseStdin`。[global version constants](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/envd/versions.py#L3-L10) [close_stdin gate/call](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/sandbox_sync/commands/command.py#L151-L180)

因此有两种诚实发布策略：

1. files-only PR且不补其他全局 gate：先全局声明 `0.4.0`，同时把 `0.6.2` 作为完成相关能力审计后的目标；
2. 同步补齐/确认 `0.5.2..0.6.2` 涉及的其他能力：直接全局声明 `0.6.2`，得到 files全参数兼容。

不要只因当前官方 envd是 0.7.0 就直接上报 0.7.0；2.46 SDK还会打开 `include_entry`、network-mount watch等 `0.6.3/0.6.4` gate，这些不属于本题。[watch gates](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/envd/versions.py#L9-L10) [watch checks](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/sandbox_sync/filesystem/filesystem.py#L654-L670)

## 8. 并发、性能与高可用约束

这些不是额外 API，但直接决定实现能否在高并发下安全上线：

1. **上传和下载必须流式转发/落盘。** file-like multipart可以没有 `Content-Length`，大文件整包缓冲会让 proxy/admin 内存随并发线性放大。应设置每请求/每 part 大小、读空闲和总并发上限，并将取消传播到后端。
2. **不要把 write当作可盲目重试的幂等请求。** 当前 SDK只重试“连接建立前、请求尚未写出”的失败，避免重放服务端可能已经收到的写入。[current connect-only retry](https://github.com/e2b-dev/E2B/blob/d42686d982f741b01f2c71da304e63846b34706f/packages/python-sdk/e2b/api/client_sync/__init__.py#L26-L48) ROCK 中间层也不应在已转发 body 后自动重试。
3. **批量 multipart不是事务。** 官方 envd顺序处理 parts；后面的 part失败时，前面的文件可能已经写入。最小兼容不要求原子批量，但必须保持确定的输入/响应顺序，并在失败时返回明确错误。
4. **目录遍历必须有背压和资源边界。** `depth` 可放大 inode walk和响应体；应限制最大 depth/entry/response bytes并产生稳定错误，而不是阻塞 event loop或耗尽内存。
5. **同一 sandbox的并发写需要明确覆盖语义。** E2B语义是 overwrite，不承诺跨请求事务；官方 envd也是截断后直接写入。ROCK 至少应复现并测试选定语义、及时关闭文件，并避免中间层重试把一次并发竞争放大成更多写入。
6. **路由与鉴权必须在读 body前完成。** 无效 sandbox id/token应尽早拒绝，避免攻击者用超大 streaming body消耗后端资源。
7. **`GET /health` 应走与文件/RPC相同的 sandbox路由。** 否则健康探测可能对错实例返回 2xx，SDK会错误保留原 transport异常。

## 9. 建议验收矩阵

每个声明 profile都分别安装/运行 `e2b==2.34.0` 与 `e2b==2.46.0`；五个 method/path的基础用例在每层重复，版本专属用例只在对应层增加。

### 9.1 所有 profile的共同用例

1. `make_dir("/tmp/a/b") is True`，第二次为 `False`；
2. `write` 分别写 UTF-8 `str` 和含 `\x00\xff` 的 bytes；
3. multipart `write_files` 覆盖空列表、单项、多项、跨目录、同名 basename不同 path；
4. `read(..., "text")` 文本一致，`read(..., "bytes")` 的 `bytearray` 逐字节一致；`read(gzip=True)` 内容一致；
5. `list(path)` 只含一层，`list(path, depth=2)` 含第二层；每项 field/type准确；
6. `get_info` 覆盖普通文件和目录，mode/permissions/owner/group/modified_time合理；
7. 不存在文件的 read/get_info/list分别抛 `FileNotFoundException`；
8. 抓包断言三个 RPC为 `application/json` 裸 JSON，body首字节直接是 `{`，不是 5-byte envelope；
9. 2.34 请求可以没有 `Connect-Timeout-Ms`，2.46 默认请求可以带 `60000`；两者均成功；
10. upload response为 `type:"file"`，RPC response为 `type:"FILE_TYPE_FILE"` / `FILE_TYPE_DIRECTORY`；
11. 大文件和并发请求验证 proxy/admin内存有界、取消及时、一个 sandbox故障不拖垮其他 sandbox。

### 9.2 `0.4.0` profile

1. 所有方法在 `user=None` 时抓包确认 REST无 `username`、RPC无 Basic auth，结果仍以 sandbox default user解析相对路径并设置 ownership；
2. 显式 `user="user"` / 允许的其他 user仍产生 query/Basic并使用所选用户；
3. binary `IO` + `use_octet_stream=None` 仍为 streaming multipart；text-mode IO在 multipart分支会先由客户端读入内存；
4. 非空 metadata由 SDK客户端拒绝，服务端不应收到请求。

### 9.3 `0.5.7` profile

1. `IO` + `use_octet_stream=None` 自动变成 `application/octet-stream`；
2. `use_octet_stream=True` 对 `str/bytes` 也走 raw upload；`False` 强制 multipart；
3. `gzip=True,use_octet_stream=False` 仍走 gzip octet-stream，并能正确解压写入；
4. multi-file IO：sync观察 N个顺序请求，async允许 N个并发请求，结果顺序仍与输入一致；
5. 对 streamed/chunked body不依赖 Content-Length，断连/超限能清理资源。

### 9.4 `0.6.2` files-full profile（推荐目标）

1. multipart与octet-stream都覆盖 non-empty metadata；
2. metadata key大小写、非法 token、非 printable ASCII、246-byte key和4096-byte总量边界；
3. `write` / `write_files` 返回的 `WriteInfo.metadata` 与实际持久化一致；
4. `get_info` / `list` 的 `EntryInfo.metadata` 可回读；
5. 覆盖时整组替换 metadata；未传 metadata时清空旧值；
6. 同一 batch每个文件得到同一 map；octet拆分请求时每个请求都有同样 headers。

### 9.5 若声明当前官方 `0.7.0`

除上述全部用例外，至少增加有效 symlink的 `get_info/list`：symlink-to-file在 2.34和2.46都应得到 `FileType.FILE`，symlink-to-dir都应得到 `FileType.DIR`，两者的 `symlink_target` 都非空且准确。还必须在 files文档之外完成所有被 0.7.0 打开的全局 gate验收。

## 10. 最终开发清单（按推荐 `0.6.2` files-full目标）

1. 增加 3 个 Connect unary handler：`MakeDir`、`ListDir`、`Stat`；
2. 增加普通 HTTP `GET /files` 与 `POST /files`；POST同时接受 streaming multipart、raw octet-stream和gzip body；
3. 复用现有 E2B sandbox routing/token链路，同时支持“显式 REST username / RPC Basic user”和“二者缺失时使用 sandbox default user”；
4. 实现 `X-Metadata-*` 校验、替换/清空持久化，以及 REST WriteInfo / RPC EntryInfo回读；
5. 统一实现 path展开、ownership、完整 EntryInfo/FileType映射和 E2B错误映射；
6. 对 async N请求 batch、multipart、raw/gzip大文件做并发限制、背压、取消和资源清理；
7. 建议同时接入 `GET /health` 故障探测；
8. 用 §9 的 2.34.0 + 2.46.0 × 0.4.0/0.5.7/0.6.2矩阵验收；
9. 在全局响应改为 `0.6.2` 前，确认/补齐同一版本值触发的非 files能力；否则阶段性全局声明 `0.4.0`。

仍然不需要独立 `write_files` API，也不需要为 `read(format="text")` 与 `read(format="bytes")` 开两个服务端接口；它们分别复用 `POST /files` 与 `GET /files`。提高 envdVersion增加的是同一路径上的 content type、headers、default-user与返回字段语义，不增加题设范围内的 method/path。
