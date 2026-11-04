# 实时调试进度

INFO 输出启动、恢复、首页、板块分页、帖子开始和逐图完成；DEBUG 补充 HTTP 请求类型、等待与重试。只记录 fid/tid、计数、耗时，不记录正文、完整 URL、Cookie 或凭据。

抓取进程将轻量运行快照原子写入数据库旁 progress.json，独立阅读进程通过 /api/debug/state 的 current_run 返回。含 PID、trace、UTC 更新时间、阶段、fid/tid、图片尝试/成功/失败和累计统计。无快照返回 null，进程死亡但状态仍 running 时标记 interrupted，避免把旧快照误认实时运行。正常结束、失败或中断都更新终态。保留原 last_run 完成统计，不依赖数据库 pending 作为下载进度。

测试：单元验证原子文件与失活进程；模块验证独立读取和脱敏字段；现有中性真实 localhost HTTP 集成回归验证抓取、阈值和Web功能。无UI变更。

## 用户追加的有限图片并发

- 新增 `[request] image_workers`，缺省4、范围1..8，1恢复串行。
- 新增 image_interval，缺省0.25秒，所有图片线程共享请求启动间隔；页面请求仍使用 interval。零间隔测试配置可省略图片间隔而同步为0。
- 每帖有界预取，保留正文图片顺序；线程池不预先下载广告、签名、回帖或受限块内图片，不为重复URL重复下载。
- 每线程独立 urllib opener，共享 CookieJar 会话、robots 缓存及受锁保护的限速；robots 获取互斥，保持权限与配额规则。
- 网络下载完成度使用 download_total/completed/failed，正文图片处理另保留 image_attempts/available/failed，不把多帧GIF重复计数。
- 线程池在异常和成功路径均关闭，历史、阈值判断与SQLite写入仍由主线程处理。

## 安装权限诊断

用户安装错误只能说明服务账号对某些路径的访问检查失败，不能凭日志断言是哪一个路径。应在服务账号身份下按 read/execute/traverse/write 分类返回失败路径；无法执行用户Python时可用系统Python进行只读访问诊断。禁止递归放宽家目录权限、强制接管其它账号文件或改用root抓取。公开文档只给通用部署路径，不保存用户私有服务器路径。
