# 🔌 模型配置（Models）

> Overfit 用到**两个完全不同的模型**。这份文档说明它们的区别、为什么一个能随便换而另一个不能、怎么配置，以及开源分发时的开放问题。
>
> 选型总表见 [TECH-STACK.md](./TECH-STACK.md)，每层职责见 [PIPELINE.md](./PIPELINE.md)。

---

## 一、两类模型，不是一回事

最容易踩的坑：以为「装了个 27B 的模型」就够了。**生成模型和 embedding 模型是两个物种，两个都需要。**

| | 生成模型 | Embedding 模型 |
| --- | --- | --- |
| **输入 → 输出** | 文字 → **文字** | 文字 → **一串数字（向量）** |
| **典型体积** | 15–30 GB | **1.2 GB** |
| **硬件要求** | 基本要显卡 | **CPU 就够** |
| **能对话吗** | 能 | **不能**，它没有生成能力 |
| **用在第几层** | 第 7 层 Generator | 第 4 层 Embedder、第 6 层 Retriever |
| **跑多频繁** | 每条命令都跑 | 建库时跑一遍，之后只 embed 查询语句 |
| **当前选型** | `qwen3.6:27b`（本地）/ `deepseek-v4-flash`（API） | `bge-m3`（本地） |

> ⚠️ 代码类模型（如 devstral）是给写代码调优的，不适合出课程考题。第 7 层用通用对话模型。

---

## 二、非对称性：一个能随便换，一个不能

这是整套配置里最重要的一条，也是所有设计决策的来源。

| | Embedding 模型 | 生成模型 |
| --- | --- | --- |
| **能不能随时换** | ❌ 换了整个向量库作废，必须重建 | ✅ 随时换，甚至可以按命令换 |
| **配置的性质** | **索引的属性**，写进库里的 meta 表 | 运行时配置，改 `.env` 即可 |
| **走不走 API** | **永久本地** | 本地调试 → 稳定后可切 API |
| **换错了会怎样** | **不报错**，检索悄悄退化成噪声 | 立刻能从产出质量看出来 |

**为什么 embedding 不能换**：不同 embedding 模型训练出的是完全不同的坐标系。建库时用 A 模型、查询时用 B 模型，算出来的距离毫无意义——但程序**不会崩**，只会返回一堆不相关的 chunk。你会以为是切分策略不好、prompt 写得烂，然后查上一整天。

**所以必须写这个护栏**：库里存 `embed_model` / `embed_dim` / `embed_runtime`，`search()` 前校验，不匹配直接报错要求重建。

```python
if meta["embed_model"] != settings.embed_model:
    raise RuntimeError(
        f"这个库是用 {meta['embed_model']} 建的，当前配置是 {settings.embed_model}。"
        f"向量空间对不上，请先 overfit reindex 重建。"
    )
```

十行代码挡掉一整类最难查的 bug。**让错误大声崩掉，比任何调试技巧都管用。**

---

## 三、为什么 embedding 选择本地，而且是永久本地

直觉上「既然锁死了，不如锁个最好的 API 模型」。但仔细想会发现——**走 API 并不能解除锁定，只是把锁交到别人手里。**

**1 · API 模型比本地模型更容易消失。** Google 的 `text-embedding-004` 已经过时，`gemini-embedding-001` 也标了废弃日期。API 端点说关就关，而开源权重下载到硬盘上，五年后还能跑。对一个「库一旦建成就不想重建」的组件，开源权重反而是更稳的选择。

**2 · 开源项目 + API embedding = 每个用户都要自己办 key。** 用户装完工具第一步是「请注册云服务并绑定信用卡」，大部分人到这里就走了。

**3 · 课件是别人的知识产权**，全量发给第三方 API 是真实的合规问题。

> DeepSeek 官方 API **没有 embedding 接口**，只有对话模型。所以对 embedding 来说，「以后切 API」这条路本来也不存在。

### 为什么是 bge-m3

- **8K 上下文窗口** —— 很多 embedding 模型只吃 512 tokens，会把超长 chunk 直接截断且**不报错**，等于把切分策略锁死在 512 以内。8K 解除了这个约束
- **跨语言检索** —— 课件是英文，但提问经常是中文（"出一套关于过拟合的题"）。bge-m3 覆盖 100+ 语言且专为跨语言检索训练，英文向的模型在这里明显吃亏
- **本地免费** —— 1.2GB，MIT 协议，CPU 可跑，不联网

---

## 四、配置

```bash
# .env

# ── 生成模型：随时可换，不影响已建的库
LLM_BASE_URL=http://localhost:11434/v1   # Ollama
LLM_API_KEY=ollama                       # 本地随便填，SDK 要求非空
LLM_MODEL=qwen3.6:27b

# ── Embedding 模型：换了必须重建整个向量库！
EMBED_BASE_URL=http://localhost:11434/v1
EMBED_API_KEY=ollama
EMBED_MODEL=bge-m3
```

切到 DeepSeek 只改生成那三行：

```bash
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=sk-xxxx
LLM_MODEL=deepseek-v4-flash
```

**为什么一个 SDK 就够**：Ollama、vLLM、LM Studio、llama.cpp、DeepSeek 全都是 OpenAI 兼容协议。所以"适配层"塌缩成三个配置项，不需要一堆 `class XxxProvider` 的继承体系。

```python
from openai import OpenAI
client = OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key)
```

**`EMBED_DIM` 刻意不放进配置**——首次建库时探测一次真实维度再写进库。任何需要人手填对的数字，迟早会被填错。

---

## 五、为什么先用本地模型调试

**弱模型是上游质量的放大镜。**

强模型容错能力强：检索回来 5 个 chunk 里混了 2 个噪声，它能自己忽略掉，照样答得像模像样——**从而掩盖了检索层的问题**。本地 27B 没这个本事，材料一乱就答崩。

所以：

- 本地跑得通 → Parser / Chunker / Retriever 是健康的
- 本地出的题很烂 → **先去看检索回来的 chunk，别急着怪模型**

### Prompt 按下限设计

本地模型遵守 JSON schema 的能力明显弱于前沿模型，所以：

- schema 保持**扁平**，别搞多层嵌套
- 指令写得像给新人交代任务，不依赖模型自己「领会」
- 生成后走一遍 **pydantic 校验 + 失败重试**

这样做的理由是**不对称的**：为弱模型写的 prompt，强模型跑起来一样好；反过来，为强模型写的精巧 prompt，弱模型直接崩。**下限决定设计，上限自然满足。**

但注意分寸——不该为了迁就弱模型而砍功能。正确做法是**优雅降级**（检测到不支持 structured output 就退回 JSON 模式 + 校验重试），而不是所有人都用最低标准。

一句话原则：**让配置承担差异，让代码保持单一。**

---

## 六、开放问题：开源分发怎么办

> 状态：**未决**。等骨架跑通、拿到真实性能数字再定。

**问题**：别人 clone 这个项目，电脑里没有任何本地模型，怎么用？

**关键事实**：embedding 模型只有 1.2GB 且 CPU 可跑，**跟跑 27B 完全不是一个量级的门槛**。而且建库只跑一次，慢几分钟可以接受。

### 三条候选路线

| | 用户要做什么 | 代价 |
| --- | --- | --- |
| **A · 内嵌 embedding + API 生成** | 只填一个 API key，embedding 首次运行自动下载权重 | 依赖体积变大 |
| **B · Ollama 全家桶** | 装 Ollama + pull 两个模型 | 门槛高，但完全免费且离线 |
| **C · 两条都支持** | 自选 | 要维护两条路径 |

路线 A 用 `fastembed`（ONNX runtime，依赖几十 MB）或 `sentence-transformers`（功能全，但要拖 torch 两个多 G）把模型跑在进程内，用户**不需要装 Ollama、不需要起服务**。安装流程能压缩成：

```bash
uv tool install overfit
echo "LLM_API_KEY=sk-xxx" > .env
overfit ingest --course IFN636
```

待确认：`fastembed` 是否支持 bge-m3（`TextEmbedding.list_supported_models()`）。

### 为什么这个决定可以推迟

**每个用户建自己的库，用户之间不共享 `.db` 文件。** 所以 embedding 运行时只需要在单个用户内部保持一致，不需要全球统一——换运行时只影响你自己那个库，重建一次即可。

更重要的是：**Embedder 是独立的一层，换运行时改一个文件，其余六层一个字都不用动。** 分发形态是打包问题，不是架构问题。

> ⚠️ 记一笔：Ollama 的 bge-m3 默认是 Q4 量化，sentence-transformers 跑 fp32，**同一个模型算出来的向量会有细微差别**。所以别混用——meta 表里除了模型名，也要记运行时。

### 效果会因人而异吗

会，但没想象中严重：

- **Embedding 侧差异很小** —— 模型都很小，任何跑得动生成模型的机器跑 bge-m3 都是零负担，而且绝大多数用户不会改默认值
- **真正的差异在生成侧** —— 但那恰好是**可以随时换**的那一层，想要更好的效果改三行 `.env` 就行，不用重建任何东西
