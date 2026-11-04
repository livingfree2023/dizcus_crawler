# 进度

- [x] SSH定位精确路径反代与后端路由不匹配；后端直连200。
- [x] 明确子路径与PWA/链接/鉴权契约。
- [x] 增加base_path配置与生成URL/路由支持。
- [x] 7项本地中性HTTP专项及Python3.12兼容验证通过。
- [x] Python3.14完整113项回归通过（33.525秒），Caddy候选已validate成功、活动配置未变。
- [x] 发布1.3.0并在目标机设置base_path、更新对应Caddy匹配；配置备份与validate后原子替换，restart阅读服务并reload Caddy。
- [x] 真实公网HTTPS与本机正确SNI/TLS均验证：入口重定向、未认证401、认证首页/API/Manifest/app.js/SW/icon全部200；原凭据保持。

## 【Handover 备忘】

1.3.0已交付部署，默认base_path空兼容根部署。用户现有子路径保留，只调整对应Caddy匹配与app base_path，不触碰其它代理应用。113项回归及Python3.12专项7项均通过；配置私有备份已保留，validate与源配置哈希检查后原子替换，阅读服务已restart、Caddy已reload。真实公网HTTPS及本机正确SNI/默认证书验证均通过：未认证401，认证首页/诊断/Manifest/app.js/SW/icon全部200，入口308规范化。实际已无本地HTML，因此没有读取真实文章正文或以其验证；文章字节和路由由中性HTTP测试覆盖。未启动抓取，不将私有域名、凭据或配置备份提交仓库。发布版本与远程同步以Git为准。无本特性待办。
