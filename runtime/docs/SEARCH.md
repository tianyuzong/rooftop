# 本地严格匹配与语义搜索

除总览外，情报研究、策略与风控、研报库页面都提供两种搜索模式：

- 严格匹配：对标题和正文做大小写无关的完整子串匹配，不加载模型。
- 语义匹配：使用 `Qwen/Qwen3-Embedding-0.6B` 在本地生成向量，适合中文金融概念、同义表达和主题检索。

选择 0.6B 版本是为了在 RTX 5080 16GB 上兼顾效果、启动速度和显存余量。模型官方支持 100 多种语言、最长 32K 上下文和 1024 维向量，并支持 Matryoshka 自定义维度；本项目使用 512 维并重新归一化，以降低 SQLite 存储和逐条余弦计算成本。参考 [Qwen3 Embedding 官方仓库](https://github.com/QwenLM/Qwen3-Embedding) 与 [官方模型卡](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)。

## Windows / RTX 5080 安装

项目当前验证组合是 PyTorch 2.12.0 + CUDA 13.0、sentence-transformers 5.7.0。Blackwell 显卡应使用 CUDA 13.0 构建；安装依据见 [PyTorch 2.12 发布说明](https://pytorch.org/blog/pytorch-2-12-release-blog/) 和 [官方历史版本安装命令](https://pytorch.org/get-started/previous-versions/)。

```powershell
Set-Location "<插件根目录>"
.\.venv\Scripts\python.exe -m pip install --force-reinstall torch==2.12.0 --index-url https://download.pytorch.org/whl/cu130
.\.venv\Scripts\python.exe -m pip install -r runtime\requirements-search.txt
```

完整交付包已经包含约 1.2 GB 的模型权重，默认从 `runtime/data_lake/models/` 加载。也可预先建索引：

```powershell
.\.venv\Scripts\python.exe -c "from app.search import sync_semantic_index; print(sync_semantic_index())"
```

`GET /api/search/status` 可检查 CUDA 设备、模型名、维度及已索引条数。索引按内容 SHA-256 增量更新；标题或正文没有变化时不会重复计算 embedding。
