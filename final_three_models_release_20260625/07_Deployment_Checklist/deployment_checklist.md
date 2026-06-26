# 最终三模型部署整理清单

## 目录整理

- [ ] 建立 `01_MARE_Final/`
- [ ] 建立 `02_Director_Final/`
- [ ] 建立 `03_SelfACQ_Final/`
- [ ] 建立 `04_Final_Selection_Report/`
- [ ] 建立 `05_Config_and_Prompts/`
- [ ] 建立 `06_Evaluation_and_Benchmark/`
- [ ] 建立 `07_Deployment_Checklist/`

## 每个模型目录应包含

- [ ] 主程序入口文件
- [ ] 模型配置文件
- [ ] prompt / policy / rule 文件
- [ ] 数据输入输出说明
- [ ] 依赖清单
- [ ] 运行命令说明
- [ ] 最近稳定版本快照
- [ ] 示例输出

## 选型确认

- [ ] MARE 采用 `System 3`
- [ ] Director 采用 `System 3`
- [ ] SelfACQ 采用 `System 1`

## 继续保留的参考样本

- [ ] 保留 `System 1 MARE` 作为保守策略参考
- [ ] 保留 `System 2 SelfACQ` 作为迁移稳定性参考
- [ ] 保留 `System 1/2 Director` 作为价格口径稳定性参考

## 上线前核对

- [ ] 收益函数定义固定
- [ ] 输入字段字典固定
- [ ] 输出字段字典固定
- [ ] 版本号固定
- [ ] Git / Drive 存档完成
- [ ] 交付文档完成
