# ShopTrace

ShopTrace 是一个面向购物任务的浏览器 Agent。模型通过一组 DOM 工具操作真实的 Chromium 页面，每轮只执行一个动作；每一步的观测、动作、截图和结果都会保存下来，可以离线回放。

**在线回放：** <https://zhengjiabei1.github.io/shopping_agent/>  
[给老师看的 3 分钟导览](docs/REVIEW_GUIDE.zh-CN.md)

![跨页采购任务回放](docs/assets/shoptrace-replay.gif)

## 功能

- **基于 DOM 的浏览器循环。** Playwright 提取可见文本和可交互元素，模型每轮只返回一个 `click`、`fill`、`select`、`press`、`scroll`、`back`、`wait` 或 `finish` 调用。元素引用只来自最新一次观测，过期引用会被拒绝。
- **三组任务。** 12 件商品商店上的 6 个基础任务；每例 8 件商品的 6 个开发案例和 6 个留出案例；24 件商品商店上的 10 个跨页采购任务，带可控的加购故障。
- **可回放轨迹。** SQLite 保存每一步、截图、动作、错误和页面记忆事件；静态 portal 不需要后端就能回放。
- **有边界的执行。** origin/path 白名单、外部任务只读、每次运行独立浏览器上下文、步数/时间/token/上下文预算、取消，以及源码变化时拒绝继续运行的指纹检查。

## 结果

合成任务使用 DeepSeek `deepseek-flash`，temperature 0，预算 35–70 步、180 秒；外部任务预算 50 步、240 秒。

| 评测 | 结果 |
|---|---|
| 6 个基础任务 × 5 次 | 30/30 通过 |
| 采购留出集 | 7/8 通过，1 未知 |
| 外部只读切片 | 12/30 通过，14 失败，4 未知 |
| 测试 | 256 项 |

外部切片是 WebVoyager 的 30 个任务子集。干净站点子集 10/15；全集包含被拦截的 Allrecipes，以及缺少 CDN 资源、页面降级的 GitHub/Coursera。该切片为只读、人工评分，不能当作 WebVoyager 榜单成绩。

采购留出集是主要的能力检查：同一商店内 8 个未见约束组合，7 个通过，1 个取消后保持未知。6 个基础任务本身很小，30/30 说明的是 harness 和页面稳定，不是通用网页能力。

## 范围与限制

- 商店是合成的（12–24 件商品），没有登录、结算、支付、优惠券、退货或多标签页流程。
- 样本很小：8 个留出案例、30 个外部任务、每种上下文设置 2 次。这些数字是工程测量，不是统计结论。
- 没有基线 Agent 对比。项目比较的是上下文和恢复设置，不是 ShopTrace 与另一个 Agent 或人类。
- 外部任务只读，但 GET 请求仍可能产生副作用，结果需要人工复核。
- 页面记忆和恢复是提示词与文本缓存，不是学习到的策略。

## 运行

需要 Python 3.11+ 和 Chromium。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-webagent.txt
python -m playwright install chromium
python -m webagent doctor
python -m webagent serve --port 8775
```

打开 <http://127.0.0.1:8775>。运行模型任务前，在服务端设置 `LLM_BASE_URL`、`LLM_MODEL`，必要时设置 `LLM_API_KEY`。密钥不会进入浏览器或轨迹。

```bash
python -m webagent tasks
python -m webagent run --task-id browser-thermos-variant --provider api
python -m webagent procurement --split development
```

静态报告在 `portal/dist/`：

```bash
python -m http.server 8790 --bind 127.0.0.1 --directory portal/dist
```

## 测试

```bash
WEBAGENT_CHROMIUM_PATH="/path/to/chrome" python -m unittest discover -s tests
```

测试覆盖浏览器边界、过期引用、origin/path 规则、预算、取消、存储、任务判分、采购故障、模型协议错误、portal 构建和部署模板。未配置 Chromium 时，浏览器相关测试会跳过。

## 目录

```
webagent/      浏览器循环、DOM 观测、任务、采购、页面记忆
lab/           SQLite 存储、模型客户端、评测辅助
portal/        静态回放页面和报告构建
benchmarks/    外部基准清单和只读切片
tests/         单元测试和浏览器测试
```

架构见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，评测见 [docs/EVALUATION.md](docs/EVALUATION.md)，外部基准见 [docs/EXTERNAL_BENCHMARK.md](docs/EXTERNAL_BENCHMARK.md)，运行与部署见 [docs/RUNNING.md](docs/RUNNING.md)、[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

实现和文字整理使用了 AI 辅助。

## 许可证

MIT，见 [LICENSE](LICENSE)。第三方声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
