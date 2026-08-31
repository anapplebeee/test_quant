# Architecture Decision Records

ADR 用于记录影响多个模块、长期存在或难以回滚的架构决策。ADR 一经接受不直接改写历史；后续决策通过新 ADR 标记 supersede。

## 索引

| ADR | 状态 | 决策 |
|---|---|---|
| [`0001-modular-monolith-workers.md`](0001-modular-monolith-workers.md) | Accepted | 保留模块化单体，拆分 Research/Trading/Scheduler Worker |

## 文件命名

```text
NNNN-short-decision-title.md
```

## 模板

```markdown
# ADR-NNNN：标题

> 状态：Proposed | Accepted | Deprecated | Superseded
> 日期：YYYY-MM-DD
> 决策者：角色或团队

## 背景

## 决策驱动因素

## 备选方案

## 决策

## 结果

### 正面影响

### 负面影响与成本

## 实施约束

## 验证与退出条件

## 参考
```
