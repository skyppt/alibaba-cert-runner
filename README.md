# Alibaba Certificate Runner

本项目用于在本机 Chrome 登录状态下批量处理 Alibaba 国际站商品：

1. 从 API 下载上架商品 ID 到 SQLite 队列。
2. 通过商品编辑页逐个处理商品。
3. 修改自定义属性 `Payment` 为 `Paypal\TT\Western Union\Trade Assurance`。
4. 关联 `CE` 和 `UL 508` 两个证书。
5. 必要时选择物流提供方式。
6. 提交商品并记录成功、失败、跳过状态。

## 当前状态

当前队列数据库已复制到：

```bash
logs/alibaba_cert_runner_506068.sqlite3
```

查看进度：

```bash
sqlite3 logs/alibaba_cert_runner_506068.sqlite3 \
  "SELECT queue_status, count(*) FROM cert_product_queue GROUP BY queue_status ORDER BY queue_status;
   SELECT status, count(*) FROM product_runs GROUP BY status ORDER BY status;"
```

## 初始化

```bash
cd /Users/simon/source/alibaba-cert-runner
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

然后把 `.env` 里的 Alibaba API 信息填好。`.env` 不会提交到 Git。

Chrome 需要保持登录 Alibaba，且开启：

```text
允许 Apple 事件中的 JavaScript
```

## 下载商品 ID

```bash
.venv/bin/python tools/alibaba_cert_queue_from_api.py \
  --db logs/alibaba_cert_runner_506068.sqlite3 \
  --read-api-total
```

如需按时间窗口重跑：

```bash
.venv/bin/python tools/alibaba_cert_queue_from_api.py \
  --db logs/alibaba_cert_runner_506068.sqlite3 \
  --start-at "2010-01-01" \
  --chunk-days 7
```

## 批量处理商品

先跑小批测试：

```bash
.venv/bin/python tools/alibaba_cert_chrome_runner.py \
  --db logs/alibaba_cert_runner_506068.sqlite3 \
  --limit 5 \
  --publish \
  --product-timeout 300 \
  --max-consecutive-failures 5 \
  --sleep-between 1
```

主批次：

```bash
.venv/bin/python tools/alibaba_cert_chrome_runner.py \
  --db logs/alibaba_cert_runner_506068.sqlite3 \
  --limit 500 \
  --publish \
  --product-timeout 300 \
  --max-consecutive-failures 10 \
  --sleep-between 1
```

重试失败队列：

```bash
.venv/bin/python tools/alibaba_cert_chrome_runner.py \
  --db logs/alibaba_cert_runner_506068.sqlite3 \
  --limit 100 \
  --publish \
  --retry-failed \
  --attempts 2 \
  --product-timeout 300 \
  --max-consecutive-failures 10 \
  --sleep-between 1
```

## 恢复旧版本

旧版本备份在：

```bash
backups/alibaba-cert-runner-20260623-232951/
```

恢复脚本：

```bash
cp backups/alibaba-cert-runner-20260623-232951/alibaba_cert_chrome_runner.py tools/alibaba_cert_chrome_runner.py
```

恢复数据库：

```bash
cp backups/alibaba-cert-runner-20260623-232951/alibaba_cert_runner_506068.sqlite3 logs/alibaba_cert_runner_506068.sqlite3
```
