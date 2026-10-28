# DBA

数据库运维相关方案与资料仓库。首期聚焦 PostgreSQL 多集群高可用，以及 Kubernetes、Ceph、备份恢复和日常运维的配合。

## 文档

- [PostgreSQL 多集群高可用方案](proposals/postgresql-kubernetes-ceph.md)：已审核的方案方向及设计取舍。
- [PostgreSQL 多集群运维架构设计](docs/postgresql/architecture.md)：高可用、一致性、存储、备份恢复和故障边界。
- [PostgreSQL 多集群部署与验收](docs/postgresql/deployment.md)：部署前置条件、示例清单、验证及故障处置步骤。
- [生产运维与告警](docs/postgresql/operations.md)：版本交接、网络矩阵、WAL 容量、切换时序、Ceph 性能和备份治理。
- [生产验收与故障演练](docs/postgresql/acceptance.md)：事务确认对账、故障场景、恢复和验收证据。
- [NetworkPolicy](examples/network/pg-a-networkpolicy.yaml)、[资源配额](examples/namespace/pg-a-quota.yaml)、[Operator 高可用补丁](examples/operator/cnpg-ha-patch.yaml)、[Operator PDB](examples/operator/cnpg-pdb.yaml)、[Prometheus 抓取配置](examples/monitoring/cnpg-pg-a.scrape.yaml)、[自定义监控查询](examples/monitoring/cnpg-pg-a.custom-queries.yaml)、[数据库告警](examples/monitoring/cnpg-pg-a.rules.yaml)和[恢复证据告警](examples/monitoring/recovery-evidence.rules.yaml)：均须替换并验证环境参数；恢复证据指标需由现有监控接入，不是组件内置指标。

文档以 CloudNativePG、Kubernetes 和独立 Ceph 为目标；仓库无已批准的环境版本锁。示例需要在实际环境中完成版本、容量、安全和故障演练后才能用于生产；仓库不包含实际环境凭据或私有配置。

## 离线检查

在仓库根目录执行以下命令；`promtool` 需与目标 Prometheus 版本匹配，规则测试不连接数据库或集群。Kustomize 渲染和 server-side dry-run 的准备与边界见[生产运维](docs/postgresql/operations.md)。

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
promtool check config --syntax-only examples/monitoring/cnpg-pg-a.scrape.yaml
promtool check rules examples/monitoring/cnpg-pg-a.rules.yaml
promtool check rules examples/monitoring/recovery-evidence.rules.yaml
promtool test rules examples/monitoring/cnpg-pg-a.rules.test.yaml
```

离线检查不能替代准入 Webhook、CNI、实际指标、数据库写入、Ceph I/O 和独立恢复演练。
