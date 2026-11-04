# 项目入口

- 遵守用户提供的全局只读基线，不修改全局规则文件。
- 当前阶段：用户已授权完整开发；生产入口为单脚本 `discuz_archive.py`，仅依赖 Python 3.11+ 标准库。
- 验证入口：`python3 -B -m unittest discover -v`；本地 HTTP 测试需要监听回环端口。
- 规格与进度：`specs/2026-11-03-discuz-archive/`。
- 图片阈值与永久跳过：`specs/2026-11-03-image-threshold/`。
- 实时诊断、有限图片并发与安装权限排障：`specs/2026-11-03-live-progress/`。
- 已归档HTML移出与历史独立：`specs/2026-11-03-moved-archives/`。
- systemd路径语法修复：`specs/2026-11-03-systemd-path/`。
- Web子路径反代：`specs/2026-11-03-web-base-path/`。
- 抓取历史与首次存档契约：`docs/DATABASE.md`。
- 架构、约定与验证：`docs/ARCHITECTURE.md`、`docs/CONVENTIONS.md`、`docs/TESTING.md`。
- 静态样例：`docs/prototype/output/`。其中内容为明确标注的中性测试素材，不代表真实抓取成功。
- 生产输出统一到根目录 `output/`；构建产物统一到 `dist/`，均不提交。
- 不将凭据、Cookie、真实归档内容或临时探针写入版本控制。

- 列表日期与分页：`specs/2026-11-03-list-publication/`（当前特性，旧时间未知不回填）。
