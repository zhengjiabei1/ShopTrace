# 项目导览

## 链接

- 在线回放：<https://zhengjiabei1.github.io/shopping_agent/>
- 代码：<https://github.com/zhengjiabei1/shopping_agent>
- 技术架构：[ARCHITECTURE.md](ARCHITECTURE.md)
- 评测报告：[EVALUATION.md](EVALUATION.md)
- 外部任务协议：[EXTERNAL_BENCHMARK.md](EXTERNAL_BENCHMARK.md)

## 项目是什么

ShopTrace 是一个浏览器 Agent 工程原型。它用 Playwright 打开网页，读取 DOM，每轮让模型选择一个动作，并记录每一步的截图、动作和错误。在线回放展示的是已经保存的运行，不是现场执行任务。

## 建议看的顺序

1. 打开在线回放，选 **Recovery comparison** 或 **Model regression**，点一条运行，拖动步骤滑块，看截图、页面 DOM 和实际动作。
2. 看 [评测报告](EVALUATION.md)，重点是基础任务、采购留出集和外部只读切片的结果与限制。
3. 看 [技术架构](ARCHITECTURE.md)，了解浏览器循环、执行边界和证据存储。
4. 如果关心外部站点，看 [外部任务协议](EXTERNAL_BENCHMARK.md)。

## 回放里看什么

- **Recovery comparison**：同一任务在 baseline、verify、adaptive 下的对比。
- **Procurement held-out**：跨页采购和故障恢复。
- **Model regression**：六个基础任务各跑五次。
- **失败运行**：看错误、截图和实际动作，确认失败不是隐藏的。
- **Step 滑块**：逐步看页面变化，而不是只看最终结果。

## 结果与边界

- 30 个真实站点上的只读任务全部尝试并评分：12 通过、14 失败、4 个未知；干净站点子集 10/15。未知主要来自 Allrecipes 拦截和 Wolfram 结果页未渲染，不是模型错误。
- 外部 WebVoyager 任务的结果在报告和证据里，不在回放页面里。
- 暂时没有真实下单、支付、登录或多标签页。
