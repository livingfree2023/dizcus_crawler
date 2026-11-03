# 约定

- 单一生产脚本，无第三方依赖。脚本之外仅存配置示例、文档、原型及测试。
- 不在生产输出中放示例数据。`tests/` fixtures 与 `docs/prototype/` 均明确标为中性测试素材。
- `ArchiveError` 表达可公开的失败原因，避免将原始网络异常、凭据或完整请求地址写入日志。
- 所有 SQL 使用绑定参数；成功历史有禁止 UPDATE/DELETE 的数据库触发器。
- 普通抓取不需要 root。systemd 写入须 root，只操作具有本工具 marker 的固定 unit 名称。
- 归档 HTML 不含脚本、远程 CSS、远程媒体或来源事件处理器。正文外链使用安全协议；Web 响应加 CSP 与 sandbox。
- 路由按数据库 ID 查文件，不接受任意文件路径。Web 服务不公开配置、数据库或 Cookie。
- 不提供刷新或强制覆盖开关；修复数据优先恢复原始备份。
- `blog/`、真实 `output/`、`data/`、本地配置、Cookie 及 token 均忽略，不进入 Git。
- 未来 CI 部署只监听生产脚本与核心依赖；docs/specs/blog/prototype 变更不触发部署。当前没有部署流水线；远端由 Git 的 origin 配置管理，不将私人仓库地址写入文档。
