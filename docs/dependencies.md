# 依赖更新工作流

后端直接依赖写在 `backend/requirements.in`，可复现的精确锁定结果写在 `backend/requirements.txt`。不要只手改锁文件。

## 更新流程

```bash
python -m pip install -r scripts/requirements-tools.txt
python scripts/update_dependencies.py
```

脚本使用固定的 pip-tools 版本重新解析依赖，随后会验证：

- `requirements.in` 的每个直接约束都有满足约束的锁定版本；
- 解析结果与提交的 `requirements.txt` 一致；
- 平台 marker 和 pip-compile 注释不会造成跨平台虚假漂移。

只检查而不改文件：

```bash
python scripts/update_dependencies.py --check
```

前端依赖由 `package.json` 和 `package-lock.json` 管理；CI 使用 `npm ci`，禁止无锁安装。

## CI 门禁

后端 job 会执行 `python scripts/update_dependencies.py --check`，锁文件漂移会直接失败。前端 job 执行 `npm ci`、构建和测试。插件 job 还会运行脚本单元测试，因此依赖更新不能绕过插件打包与 immutable identity 门禁。

Dependabot 每周检查 npm、pip 和 GitHub Actions，限制同时打开的 PR 数量并按生态分组；所有升级仍必须通过完整 CI，重大升级不得直接部署生产。

## 安全边界

升级时不得把生产 `.env`、数据库密码、Redis 密钥、OAuth secret、插件 HMAC secret 或真实 API token 写入 requirements、lock、日志或示例。Django 5.2.x、DRF `>=3.17.2,<3.18` 安全基线和现有生产 PostgreSQL/Redis 约束保持不变。
