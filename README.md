# DBA

数据库运维相关方案与资料仓库。首期聚焦 PostgreSQL 多集群高可用，以及 Kubernetes、Ceph、备份恢复和日常运维的配合。

## 文档

- [PostgreSQL 多集群高可用方案](proposals/postgresql-kubernetes-ceph.md)：已审核的方案方向及设计取舍。
- [PostgreSQL 多集群运维架构设计](docs/postgresql/architecture.md)：高可用、一致性、存储、备份恢复和故障边界。
- [PostgreSQL 多集群部署与验收](docs/postgresql/deployment.md)：部署前置条件、示例清单、验证及故障处置步骤。

文档以 CloudNativePG、Kubernetes 和独立 Ceph 为目标，示例需要在实际环境中完成版本、容量、安全和故障演练后才能用于生产；仓库不包含实际环境凭据或私有配置。
