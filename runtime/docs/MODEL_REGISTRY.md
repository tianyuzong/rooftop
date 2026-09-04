# 研究模型注册表

Rooftop 的 MVP 使用版本化、声明式 JSON 保存自定义财务与估值模型。模型定义进入 `research_model_definitions`，激活范围进入 `research_model_assignments`。运行时不执行用户 Python、JavaScript、Shell、Excel 宏或券商代码。

## 生命周期

1. `POST /api/models` 创建不可变版本，初始状态为 `DRAFT`。
2. 维护人检查字段、阈值、缺失值规则和版本号。
3. `POST /api/models/{id}/activate` 必须同时提供 `approved_by` 和 `confirmed=true`。
4. 激活时按 `model_kind + scope_type + scope_value + profile` 原子归档旧分配，再启用新分配。
5. 历史定义、校验和、创建人、批准人和时间均保留，不能用每日行情静默改写。

作用域优先级为 `SYMBOL > INDUSTRY > DEFAULT`。风险档位为 `aggressive / balanced / conservative`；未指定档位的估值模型可被三档共同使用。

## MVP JSON 格式

财务和估值模型使用已知字段、上下界和方向：

```json
{
  "model_key": "valuation.baijiu",
  "model_kind": "valuation",
  "name": "白酒估值模型",
  "version": "1.0.0",
  "created_by": "analyst",
  "specification": {
    "dimensions": {
      "value": [
        {"field": "valuation.pe_ttm", "low": 12, "high": 45, "inverse": true}
      ]
    }
  }
}
```

支持直接字段和两个字段的比率；`low` 必须小于 `high`。缺失值保持缺失并降低覆盖率。后端会强制写入 `research_only=true` 和 `order_execution=false`。

当前财务评分运行时消费 `financial` 与 `valuation` 模型。`factor`、`strategy`、`risk` 已有版本和审批存储契约，但在接入新的可执行研究引擎前只作为声明式研究定义保存，不能宣称已影响正式信号。

## Excel 与 Skill

Excel 可以作为人工建模工作表，但不得直接执行宏或把工作簿当作生产模型。应先把命名字段、公式结果和阈值转换为上述 JSON，经过字段白名单、范围校验、版本审批和样本外回测后再激活。

`SKILL.md` 负责稳定的路由、安全边界和输出契约，不保存每日变化的模型权重。行情、参数和模型版本属于数据库中的运行制品。
