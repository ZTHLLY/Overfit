# 🛠️ 技术栈（Tech Stack）

> 这是 Overfit 的选型说明与理由。想看整体架构去 [ARCHITECTURE.md](./ARCHITECTURE.md)，想看每一层做什么去 [PIPELINE.md](./PIPELINE.md)，想看模型怎么配去 [MODELS.md](./MODELS.md)。


Overfit 是一个轻量、自己手写、透明可控的 RAG pipeline。整套栈的气质是：**本地优先、单文件存储、不依赖重型框架**，确保每一层都看得见、改得动。

| 层 | 选型 | 说明 |
| --- | --- | --- |
| **语言** | Python 3.11+ | RAG 生态最完整 |
| **包管理** | `uv` | 现代、快、替代 pip/poetry |
| **CLI 框架** | Typer | 基于类型注解的命令行 |
| **配置管理** | pydantic-settings + `.env` | 管理 base_url / model / api_key |
| **PDF 解析** | pypdf | 轻量，输出带 page 元数据的文本 |
| **Chunking** | 自己写 | 按字符数近似切分，保留 source + page 元数据 |
| **Embedding** | **Ollama + `bge-m3`**（1024 维） | 本地、免费、8K 上下文、跨语言检索 |
| **向量存储** | SQLite + `sqlite-vec` | 单文件、零运维、可随项目 commit |
| **检索** | 余弦相似度 top-k | MVP 不做 re-ranking |
| **LLM 生成** | **本地 27B**（Ollama）→ 可切 **DeepSeek** | 通过 base_url 切换，不改代码 |
| **模型 SDK** | `openai` | Ollama / vLLM / DeepSeek 全是 OpenAI 兼容协议 |
| **结构化输出** | JSON schema + pydantic 校验 + 重试 | 本地模型遵守率较低，必须有兜底 |
| **Prompt 模板** | Jinja2 | 模板与代码分离 |
| **缓存** | SQLite（与向量库共用一个 db 文件） | 解析结果 + embedding 都缓存，避免重复计算 |

> 📌 **生成模型和 embedding 模型是两个物种，而且一个能随便换、另一个换了整库作废。** 这条非对称性是很多设计决策的来源，单独写在 [MODELS.md](./MODELS.md)。

**刻意不用的东西：**

- ❌ **LangChain / LlamaIndex** — 过度抽象，挡住 RAG 细节，也会挡住"溯源"这个核心能力
- ❌ **重量级 provider 抽象层** — 因为 Ollama / vLLM / DeepSeek 都是 OpenAI 兼容协议，"适配层"塌缩成三个配置项（`base_url` / `api_key` / `model`），不需要一堆 `class XxxProvider` 的继承体系
- ❌ **re-ranking / hybrid search / query rewriting** — 留给 MVP 之后的进阶路线

> 📝 早期版本这里写的是"❌ 多 LLM provider 抽象 —— YAGNI"。那个判断在当时是对的，现在过期了：YAGNI 反对的是为**想象中**的需求做抽象，而本地模型和 DeepSeek 是两个**当下就要用**的真实场景。

---
