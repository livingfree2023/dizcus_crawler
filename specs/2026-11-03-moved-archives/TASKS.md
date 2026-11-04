# 进度

- [x] 确认需求与身份/Basic认证实现。
- [x] 修改抓取、检查和Web缺失文件处理，文件先打开再响应避免并发移走的响应竞争。
- [x] Python3.14完整104项通过，更新文档并完成发布前检查；提交与推送状态以Git为准。

## 【Handover 备忘】

1.2.1已完成。移动HTML不影响永久历史，不计抓取失败、不重抓；--check仅列missing_files，仍检查路径安全和已有文件摘要。Web missing404不影响其它页面。身份文件和数据库仍需保留，不能删除。Basic用户名archive，密码来自token_file原始文本strip，不是路径或Caddy哈希。Python3.14完整104项通过，Python3.12受影响链路补充验证；提交与远程状态以Git为准。
