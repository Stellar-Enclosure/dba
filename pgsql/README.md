# PostgreSQL

PostgreSQL 多集群运维资料、配置示例和离线检查工具。返回[数据库目录](../README.md)。

## 文档

- [PostgreSQL 多集群高可用方案](proposals/postgresql-kubernetes-ceph.md)：已审核的方案方向及设计取舍。
- [PostgreSQL 多集群运维架构设计](docs/architecture.md)：高可用、一致性、存储、备份恢复和故障边界。
- [PostgreSQL 多集群部署与验收](docs/deployment.md)：部署前置条件、示例清单、验证及故障处置步骤。
- [生产运维与告警](docs/operations.md)：版本交接、网络矩阵、WAL 容量、切换时序、Ceph 性能和备份治理。
- [生产验收与故障演练](docs/acceptance.md)：事务确认对账、故障场景、恢复和验收证据。
- [NetworkPolicy](examples/network/pg-a-networkpolicy.yaml)、[资源配额](examples/namespace/pg-a-quota.yaml)、[Operator 高可用补丁](examples/operator/cnpg-ha-patch.yaml)、[Operator PDB](examples/operator/cnpg-pdb.yaml)、[Prometheus 抓取配置](examples/monitoring/cnpg-pg-a.scrape.yaml)、[自定义监控查询](examples/monitoring/cnpg-pg-a.custom-queries.yaml)、[数据库告警](examples/monitoring/cnpg-pg-a.rules.yaml)、[恢复证据告警](examples/monitoring/recovery-evidence.rules.yaml)和[业务/通知证据告警](examples/monitoring/service-evidence.rules.yaml)：均须替换并验证环境参数；恢复证据指标需由现有监控接入，不是组件内置指标。

文档以 CloudNativePG、持续升级的最新稳定 Kubernetes 和独立 Ceph 为目标；每次变更记录精确版本并核对兼容矩阵，仓库不代表现场版本已通过验收。示例需要在实际环境中完成版本、容量、安全和故障演练后才能用于生产；仓库不包含实际环境凭据或私有配置。

旧主隔离必须覆盖[控制面分区而业务仍可连接原主](docs/acceptance.md#41-旧主与控制面分区的强制验收)；Lease、Service 切换和 RWO 不能单独证明安全。自定义业务、恢复和送达指标需接入实际采集与终端回执，规则文件不是采集器。

## 离线检查

在仓库根目录执行以下命令；`promtool` 需与目标 Prometheus 版本匹配，规则测试不连接数据库或集群。Kustomize 渲染和 server-side dry-run 的准备与边界见[生产运维](docs/operations.md)。

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s pgsql/tests -v
promtool check config --syntax-only pgsql/examples/monitoring/cnpg-pg-a.scrape.yaml
promtool check rules pgsql/examples/monitoring/cnpg-pg-a.rules.yaml
promtool check rules pgsql/examples/monitoring/recovery-evidence.rules.yaml
promtool check rules pgsql/examples/monitoring/service-evidence.rules.yaml
promtool test rules pgsql/examples/monitoring/cnpg-pg-a.rules.test.yaml
```

离线检查不能替代准入 Webhook、CNI、实际指标、数据库写入、Ceph I/O 和独立恢复演练。
