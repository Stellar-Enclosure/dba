# PostgreSQL 多集群生产运维与告警

本文补充[架构设计](architecture.md)和[部署文档](deployment.md)中的运行参数、故障处理和验收依据；故障演练步骤见[验收手册](acceptance.md)。阈值均为初始示例，必须按业务、采集周期、容量和演练结果调整。

## 1. 版本与交接基线

目标平台按**Kubernetes 和 Ceph 最新稳定版本并持续升级**维护，不把旧参考组合当作环境上限，也不使用滚动 `latest` tag 部署数据库。每次部署/升级仍须生成一份有日期的精确版本记录：升级前后版本、镜像 tag/digest、Chart/CRD 摘要、内核与驱动、支持矩阵来源、变更单和验收结果。仓库没有实际环境的补丁版本与验收记录；“最新”是更新策略，不能代替可重现的版本记录。

| 组件 | 2026-10-30 文档核对与示例接口 | 持续维护要求 |
| --- | --- | --- |
| Kubernetes | 官方发布页列出 1.37.1；以现场最新稳定版的精确补丁为准 | 记录控制面/kubelet/kubectl、CNI、DNS、CSI sidecar 及 API 路径；核对版本偏差与 API 变化 |
| Ceph | 官方发布索引最新稳定系列为 Umbrella 21.2.0；旧示例参考 Tentacle 20.2.4 | 记录实际 Ceph/内核客户端/CSI/RBD 特性组合、FSID 和 CRUSH 域；按官方受支持路径滚动升级 |
| CloudNativePG | 现有清单/参数参考 1.30.1；1.30 支持矩阵将 K8s 1.37 列为 tested, but not supported | 应选正式支持目标 K8s 的维护版本并校验 CRD/Webhook/实例管理器；“已测试”不等于官方支持，计划发布日期不等于已发布 |
| PostgreSQL | 示例接口为 18.6，使用 CNPG 兼容镜像 | 在所选 major 中跟进受支持补丁与扩展；更换 major 必须单独迁移验收 |
| Barman Cloud Plugin | 示例接口为 0.15.1 | 核对 CNPG/PG/S3 兼容性，同时记录插件 Deployment 和 `SIDECAR_IMAGE` |
| Ceph-CSI | 旧示例参考 3.18.1；不由旧 Kubernetes 测试范围推断最新平台兼容 | 每次对照所选正式版本的支持矩阵、内核、sidecar、RBD 映射/扩容/重挂验证 |
| Prometheus / Alertmanager / kube-state-metrics / Grafana | 环境版本未记录；规则为原生 Prometheus 文件 | 记录实际版本、标签允许列表、规则加载/错误、自监控、路由和仪表盘变化 |

来源：[Kubernetes 发布页](https://kubernetes.io/releases/)、[Ceph 发布索引](https://docs.ceph.com/en/latest/releases/)、[CNPG 支持矩阵](https://cloudnative-pg.io/docs/1.30/supported_releases/)、[Ceph-CSI 支持矩阵](https://github.com/ceph/ceph-csi/blob/v3.18.1/README.md)、[Barman 插件](https://cloudnative-pg.io/plugin-barman-cloud/docs/intro/)。这些是查阅时的官方资料，**不代表现场版本或兼容验收**；未来升级按届时正式发布资料重新核验。若最新平台超出所选数据库组件的正式支持范围，应登记兼容缺口，选择受支持组件或按组织流程完成额外验证及风险接受，不能沿用旧证据直接判通过。

### 1.1 每次平台升级的检查与回退

1. 记录当前/目标完整组合和混合版本阶段；检查 Kubernetes 版本偏差、API/准入变化、CNPG/插件/CSI 支持矩阵、Ceph 升级路径和不可逆步骤。保留上一组合的配置与恢复物料；恢复方案以备份和新目标重建为基础，不假定 Ceph、CRD 或数据格式可以原地降级。
2. 在相同组合的测试环境先过离线渲染、服务端准入和实际备份恢复，再做低风险集群试点。K8s、CNI/内核、Ceph、CSI、Operator、PG、插件分批变更，每批恢复基线后再继续，保留 N-1 节点/副本与容量余量。
3. 每批检查 `.spec.instances` 与期望实例规则一致、三实例 Ready、逐实例指标、同步备库、独立业务写入、TLS、WAL 归档、PVC/Ceph 延迟与恢复链。重新验证告警 firing/恢复及最终接收方回执；标签/指标改名不能被当作无告警。
4. 对 K8s、CNI/内核、CNPG 实例管理器或存储/隔离路径的变化，重新执行[旧主分区验收](acceptance.md#41-旧主与控制面分区的强制验收)；新组合的恢复链须有 PITR 验证。旧证据保留归档，不能自动继承为新组合通过。异常时停止下一批，按该变更受支持的回退/重建路径恢复并核对 ACK。

## 2. 网络与最小权限

[示例 NetworkPolicy](../examples/network/pg-a-networkpolicy.yaml)选中 `dba-a` 中 `cnpg.io/cluster=pg-a`、`app.kubernetes.io/name=postgresql`、`app.kubernetes.io/component=database` 的 Pod；在 CNPG 1.30.1 中这些标签覆盖数据库实例和该集群的 initdb/join/recovery Job。实例/Job 互通使用同 Namespace 的同集群标签；在没有额外放行策略且 CNI 正确实施的前提下，不能访问同空间其他数据库集群。

| 源 → 目标 | 目的端口 | 用途与验收 |
| --- | --- | --- |
| 同集群实例与 Job ↔ 同集群实例 | TCP 5432、8000 | PostgreSQL 复制/初始化以及实例状态、主库隔离探测；两个方向均验证 |
| CNPG Operator → 数据库实例 | TCP 5432、8000 | 管理连接与状态；示例以 Operator Namespace 和 Pod 标签同时限定来源 |
| 授权业务客户端 → 数据库 | TCP 5432 | 仅指定 Namespace 和 Pod 标签；数据库角色及 HBA 另行限制 |
| Prometheus → 数据库实例 | TCP 9187 | 实例 metrics；限制监控来源，不能暴露到业务网络 |
| 数据库实例/Job → Kubernetes API | 实际 Service/VIP/后端的 TCP 443 或 6443 | Lease、状态和控制面；示例使用占位 CIDR，按 CNI 转换前后的真实路径替换 |
| Operator、Barman 插件、Prometheus → Kubernetes API | 实际 Service/VIP/后端的 TCP 443 或 6443 | 协调、插件资源、Pod 发现；各自 Namespace 的 egress 和 RBAC 独立限制 |
| 数据库实例/Job → DNS | UDP/TCP 53 | 示例选择 CoreDNS Pod；NodeLocal DNSCache、hostNetwork 或其他 DNS 实现须单独验证和调整 |
| Operator、插件及监控 → DNS | UDP/TCP 53 或实际 DNS 端口 | 解析 API、插件和受控外部端点；按真实部署和节点路径验证 |
| 数据库实例/恢复 Job → 独立对象存储 | 实际 HTTPS 端点及 TCP 443 | WAL、基础备份和恢复；标准 NetworkPolicy 不能按 FQDN 授权，DNS 或 LB 地址变更须维护 CIDR |
| 数据库实例/Job → 凭据或密钥服务 | 实际身份端点及 HTTPS 端口 | 仅当所选云身份、STS、KMS 或密钥代理确实需要时按目标授权；示例静态受限身份不默认放开 |
| API server → Operator webhook | Service 443 → Pod 9443 | 由平台/Operator Namespace 的策略放行；校验控制面源地址与 CNI 行为 |
| Operator → Barman 插件 | 实际插件 Service 9090，mTLS | 由平台/Operator 策略放行；插件 sidecar 与实例管理器在同 Pod 内使用 Unix socket |
| cert-manager ↔ API server | 实际 API 和 webhook Service/Pod 端口 | 插件/Operator 证书签发、准入和续期按平台已有策略验收；不同安装版本端口可能不同 |
| CSI 节点插件 → Ceph MON/OSD | 按 Ceph 交接端口及源节点 | 属于节点/CSI 数据路径，不由数据库 Pod NetworkPolicy 代替 |

示例中的 `192.0.2.10/32` 和 `198.51.100.20/32` 是保留的文档地址；必须替换为经核定的 API 与对象存储端点，不能通过全网放行绕过测试。Operator、插件和应用 Namespace 如有 egress/ingress 限制，还须由其所有者分别配置对应反向规则；数据库侧的允许规则不能解除对方的拒绝。其他 NetworkPolicy 与本策略是**并集**，已有宽松规则可能扩大访问；NetworkPolicy 不是防止标签伪造或节点侧访问的权限边界。IPv6、hostNetwork、NodeLocal DNS、Service DNAT、API server 到 webhook、Prometheus 和对象存储的实际路径均须在所选 CNI 上做正反向测试。

## 3. WAL、复制槽与容量

### 3.1 区分保留原因

PostgreSQL 18 的 `max_slot_wal_keep_size` 默认 `-1`；示例集群设置 `4GB`，这是**供演练的槽保留预算**，不是通用生产值。PostgreSQL 在 checkpoint 时评估槽可保留的 WAL；超过后槽可能变为 `unreserved` 或 `lost`，备库可能需要从归档补齐或重建。该参数不限制归档失败、checkpoint、`wal_keep_size`、正在写入、备份所需 WAL 或文件系统其他数据产生的占用，因此不能当作 WAL 或 PVC 的硬上限。[PostgreSQL 复制参数](https://www.postgresql.org/docs/18/runtime-config-replication.html)

CNPG 的 HA 物理槽默认启用并由 Operator 管理；应用自己的物理槽和逻辑 CDC 槽另登记所有者、消费端、恢复目标、保留预算和删除责任人。`idle_replication_slot_timeout` 在 PostgreSQL 18 默认 `0`，如要启用须评估 checkpoint 生效时机以及失效后的消费端重建；不要随意启用或删除 CNPG 管理槽。逻辑槽的 failover 同步、`sync_replication_slots`、`hot_standby_feedback` 和 `synchronized_standby_slots` 需要单独的 CDC 设计及演练，不能假定物理 HA 槽已覆盖。

### 3.2 预算和预测

先在代表性高峰期间按 `pg_stat_wal.wal_bytes` 和 `stats_reset` 计算 WAL 峰值生成率，再分别确定允许备库/消费端离线时间、归档端故障处理时间和重建窗口。槽预算至少覆盖所选峰值速率 × 离线窗口及余量；如果超过槽预算，必须明确接受备库重建成本。各槽通常保留同一 WAL 序列，不能把每个槽的 LSN 差机械求和。

共享数据卷时，可先用保守公式核对：`PVC 容量 × (1 - 最低空闲比例) >= 数据峰值 + 槽保留预算 + 归档故障预算 + checkpoint/备份/临时文件余量`。槽与归档可能保留重叠 WAL，上式有意保守；不能用它替代峰值压测和文件系统实际占用。例：20 GiB PVC、最低空闲 20%、数据峰值 8 GiB、槽 4 GiB、归档故障预算 2 GiB、其他余量 1 GiB，合计 15 GiB 小于允许使用的 16 GiB；仍需证明真实峰值、恢复速度与 Ceph 后端余量足够。WAL 30 MiB/min 持续 60 分钟约 1.76 GiB；速率升高或修复超时会迅速突破预算。对三实例和 Ceph 三副本再次核算实际已用数据、写入、快照和重建空间。

初始告警：槽 LSN 差大于 3 GiB 持续 5 分钟为 warning（仅针对示例 4GB 配置）；PVC 空闲低于 20% 持续 5 分钟为 warning，低于 10% 持续 2 分钟为 critical。另以最近 6 小时的可用空间变化预测耗尽时间；不足 4 小时立即升级处置。预测仅在样本充足且增长持续时使用，不能替代绝对水位，短期波动和扩容后须重新判断。

### 3.3 排查和恢复

在[部署文档第 6 节](deployment.md#61-主备卷与-service)核验 context、Namespace、Cluster 和主 Pod 后，以下是只读诊断：

```bash
k -n "$NS" exec "$PRIMARY" -c postgres -- psql -X -U postgres -d postgres -v ON_ERROR_STOP=1 \
  -c 'SHOW max_slot_wal_keep_size' -c 'SHOW wal_keep_size' -c 'SHOW max_wal_size' -c 'SHOW archive_timeout' \
  -c 'SELECT now(), wal_bytes, stats_reset FROM pg_stat_wal' \
  -c 'SELECT slot_name, slot_type, active, wal_status, invalidation_reason, inactive_since, pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained_distance, pg_size_pretty(safe_wal_size) AS safe_remaining FROM pg_replication_slots ORDER BY slot_name' \
  -c 'SELECT archived_count, last_archived_wal, last_archived_time, failed_count, last_failed_wal, last_failed_time FROM pg_stat_archiver'
```

`safe_wal_size` 为 `NULL` 不等于 0：槽已 lost 或未设置槽限制时均可能为空；`restart_lsn` 为空也不能推断零积压。`wal_status=unreserved` 可能恢复，也可能在后续 checkpoint 变为 `lost`；`lost` 的槽不可继续使用。先关联 `pg_stat_replication`、`pg_stat_wal_receiver`、实例日志和备份插件日志，确认是槽消费落后、归档失败还是二者同时发生。

备库延迟应同时观察主库 `flush_lsn`/`replay_lsn` 字节差、备库接收/回放位置、WAL 接收器及 `pg_stat_database_conflicts`；低写入时单独用最后事务回放时间可能误报。长只读查询可能与 WAL 回放冲突并被取消；增大 `max_standby_streaming_delay`/`max_standby_archive_delay` 会增加可能的回放等待，`hot_standby_feedback` 可能增加主库表膨胀。以业务可接受的查询取消、读取陈旧度和膨胀预算评估，不能默认设为无限等待或把任何慢查询都归因于网络。

1. **槽滞后**：确认槽类型和所属集群/CDC 消费者，检查网络、备库回放、事务和 Ceph I/O，优先恢复消费或按受控流程扩容。不要直接执行 `pg_drop_replication_slot`、推进槽或删除 WAL；逻辑槽丢失可能要求下游重建和业务对账。
2. **归档失败**：核对待归档 `.ready` 数量、失败时间、插件、对象存储空间/权限、TLS 和网络。修复后观察连续归档、对象读取和独立恢复点，必要时限制业务写入并扩容；不得删除未归档 WAL、关闭归档或把槽上限当作解决办法。无写入时最后归档时间偏旧不一定是故障，应结合 WAL 生成和受控探针判断。
3. **备库缺 WAL/槽 lost**：先确认现存归档和时间线是否能补齐；确认主库、同步备库和备份可恢复，核对目标 Pod/PVC/owner UID 与实际主、目标主均不同，隔离旧节点及卷后，再按该版本 CNPG 受控重建备库并核对 LSN、同步状态和数据。容量不足或无法保证剩余同步副本时先扩容/补节点，不降低数据保护要求。`kubectl cnpg destroy` 默认会删除目标实例的 PVC；`--keep-pvc` 仅将 PVC 脱离管理并保留，可能造成名称/容量冲突，不能假定自动重建成功。`alpha.cnpg.io/unrecoverable` 会删除 Pod/PVC，且节点失联时可能强制删除 Pod；两者均不可作为默认自动修复。原 PV 的 `Retain`、实际 RBD 数据、清理和重新接管须单独核验。[CNPG 副本重建边界](https://cloudnative-pg.io/docs/1.30/labels_annotations/)

## 4. 切换时序、RTO 和 RPO

CNPG 1.30.1 的默认 `smartShutdownTimeout=180s`、`stopDelay=1800s`、`switchoverDelay=3600s`，主 Lease 默认 duration/renew/retry 为 `15/10/2s`；示例仅将 smart 阶段设为 `0`，其余不为追求速度擅自调小。节点 NotReady 检测、API、隔离探测、Lease、旧主停机/归档、候选追平、Service 端点和客户端重连的耗时有重叠也可能串行；必须记录实际时间线，不能将各默认值相加当作保证值。`switchoverDelay` 是旧主 fast shutdown 的等待上限；旧主 PostgreSQL 不可达时可能跳过该阶段，不能将其当作等待 WAL 归档的保证。[实例终止](https://cloudnative-pg.io/docs/1.30/instance_manager/)、[自动切换](https://cloudnative-pg.io/docs/1.30/failover/)

| 场景 | RTO/RPO 验收口径与处理 |
| --- | --- |
| 单主进程/Pod 或单节点故障；两个备节点均可仲裁且旧主隔离已证明 | 从注入/故障时间到客户端首个成功提交目标 ≤5 分钟；已确认同步事务目标零丢失，分别记录未知结果事务 |
| 单备节点故障 | 主库及至少一个同步备节点正常时应继续写；恢复第三副本的时间单独测量，不套用 5 分钟写入目标 |
| 主库加一个备节点、仲裁不足 | 示例 `R+W=N`，默认不自动晋升；RTO 依赖恢复候选或人工决策，不能承诺 5 分钟，也不得强制切换来冒充零丢失 |
| Kubernetes API/Operator 不可用或旧主网络分区 | 不假设能选主或隔离；先验证客户端、Lease 和旧主，再恢复控制面/隔离；RTO 按事件测量，无法证明安全时不接入写流量 |
| Ceph/存储故障、对象存储或整站不可用 | 多套数据库可能同时受影响；PITR/异地恢复 RPO 取决于独立站点的实际连续 WAL，RTO 按数据量、吞吐与对账测量 |

`failoverQuorum` 在参考版仍需按实验功能管理，旧注解会覆盖 spec，`kubectl cnpg promote` 可以绕过仲裁；只有有效策略、完整候选、旧主 fencing、会话同步提交和底层持久化共同成立的已演练场景，才可按目标报告 RPO。Lease 过期、Service 已改端点和 RWO/RWOP 卷均不能单独证明旧主停止写入；每个实例本来就有独立 RBD，新主不需要争夺旧主的卷。必须执行[旧主与控制面分区、业务仍能连接旧主的验收](acceptance.md#41-旧主与控制面分区的强制验收)，以旧主停止/拒绝写入、新主完成提升和首次成功提交的时间线证明没有两个可写主；外部节点/网络隔离要在实际故障路径验证。

## 5. Operator 与多集群变更

[Operator 补丁](../examples/operator/cnpg-ha-patch.yaml)针对 CNPG 1.30.1 的官方 Deployment：2 副本、`--leader-elect` 保持来自基础清单、hostname 必需反亲和、`maxUnavailable: 1`/`maxSurge: 0`，并设置示例 requests/limits 和延迟；[PDB 示例](../examples/operator/cnpg-pdb.yaml)要求至少一个副本可用。至少两个符合 `dba.example/operator-eligible=true` 的独立节点，以及单节点故障后资源余量；两副本不是双 leader，Lease/API 故障仍可阻断协调。PDB 只约束自愿驱逐，不约束 Deployment 直接滚动删除，也不能代替备份或节点隔离。

这是 strategic merge 补丁，不能单独 `kubectl apply`。在受控空目录放入已核验的 1.30.1 官方完整清单 `base.yaml`、补丁 `ha-patch.yaml` 和 PDB `pdb.yaml`，再创建以下 `kustomization.yaml` 并离线渲染：

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - base.yaml
  - pdb.yaml
patches:
  - path: ha-patch.yaml
```

```bash
kubectl kustomize '<受控渲染目录>' > '<受控目录>/cnpg-rendered.yaml'
```

对渲染结果核对 Namespace、全部 CRD/RBAC/Service/镜像、`--leader-elect`、镜像 digest、两个节点及资源预算，再按[部署文档第 3 节](deployment.md#31-operator)的首次安装或已有组件接管边界处理。Operator 镜像和 `OPERATOR_IMAGE_NAME` 都需指向经验证的制品；插件 Deployment 镜像及其 `SIDECAR_IMAGE` 也需核对，不能留下测试镜像或滚动 tag。渲染不等于已安装。

`CLUSTERS_ROLLOUT_DELAY` 和 `INSTANCES_ROLLOUT_DELAY` 单位秒，默认均为 `0`，分别控制不同集群及同集群实例的滚动启动间隔；示例 `120/30` 仅用于压测。1.30.1 的协调状态在 Operator 进程内，延迟是节流，不保证前一次实例已 Ready、备份完成，也不是持久的全局互斥或维护窗口。Operator 切换/重启后不能依赖旧计时；`primaryUpdateStrategy: supervised` 只暂停需要人工处理的主实例更新，不阻止故障自动切换，也不阻止所有备节点更新。按版本说明先做一套低风险集群，核对备份、所有实例、同步、网络和告警后再进入下一套；不同集群备份窗口错开且核算持续时间，示例 02:00/03:00 不保证长备份不重叠。

## 6. Ceph 性能与容量

每实例一个 RBD，使用经平台验收的 RWO、`Retain` 和可扩容 StorageClass；CSI 以及 Ceph Pool/CRUSH/CephX 的生命周期由平台管理。`size=3`、`min_size=2` 等必须以实际已批准池为准，不能为保写入盲目降低 `min_size`；Ceph 节点和数据库节点的物理宿主、数据存储及网络关联必须列入同一故障域图。Ceph 副本不是 PostgreSQL 备库、快照不是独立备份。

三份 PostgreSQL 数据叠加三份 Ceph 副本，容量粗估为约 9 倍业务实际数据，WAL、数据库膨胀、快照和未回收的 Retain RBD 另计；这不是固定的 I/O 放大系数。同步事务路径还涉及主/备 WAL 刷盘、Ceph 副本确认和共享网络；测量应用 TPS/提交 P50/P95/P99、WAL 生成速率、PVC 文件系统可用量、RBD 前台时延、OSD 平均读写延迟、队列及重建流量。OSD 端平均值不能当作 PostgreSQL 提交 P99。[Ceph 监控指标](https://docs.ceph.com/en/tentacle/mgr/prometheus/)

在隔离环境分别运行正常负载、单 OSD 故障、恢复/重平衡、基础备份、恢复读取和多集群错峰/重叠组合。记录 Ceph HEALTH/PG、OSD down/out、回填速率、各层延迟、已确认事务和客户端错误；达到池容量上限、剩余副本不足、时延超过业务门限或控制面受挤占时停止演练并恢复。建议预留重建空间并按平台既定阈值告警，不以固定 70% 覆盖不同硬件和 CRUSH 故障域。

## 7. 备份与独立恢复验证

基础备份、WAL 归档、异地副本和恢复验证是四个独立状态：① `Backup.status.phase=completed`；② 主库归档连续成功且时间线和所需 WAL 可读；③ 目标独立故障域完整接收可恢复的基础备份/WAL/元数据；④ 在新集群按时间点恢复并校验业务数据。任意环节失败均不能宣称可恢复。每日备份和 7/30 天保留只是起点；记录恢复窗口、WAL 生成率、备份耗时、传输速率、异地滞后、最久可接受 RPO 及 RTO。

建议每套集群至少按约定频率做 PITR，且周期性在独立 Kubernetes、存储和对象副本环境做完整恢复；频率由业务批准，初始建议每月 PITR、每季度跨站恢复。Barman 配置中的 `data.jobs` 是单次基础备份的上传并行数，`wal.maxParallel` 是 WAL 归档或恢复的并行数；它们不提供跨集群备份互斥。示例均为 1，调整时同时测量备份窗口、归档积压与恢复时间。备份凭据、恢复只读身份、CA、密钥、DNS/镜像和文档须在源站失效时仍可获得。异地复制必须保证基础备份、备份结束所需 WAL、目标时间点之后必要 WAL、历史 timeline 和元数据都可用；同集群 RGW 和同宿主备份不构成独立灾备。

`barman_cloud_cloudnative_pg_io_first_recoverability_point` 是**最早**恢复点，不能用作最新 WAL 或异地 RPO；`last_available_backup_timestamp` 是最近基础备份，不证明后续 WAL、异地复制或恢复成功。独立站点最新可恢复时间需由可读取的连续归档与受控恢复/事务标记验证。通过平台既有监控记录最近成功跨站恢复时间、最近验证的独立恢复点、备份/复制失败及其年龄；若没有采集器或自动验证，明确列为人工值班检查与未自动化项，禁止用缺失指标生成绿色状态。

[恢复证据规则](../examples/monitoring/recovery-evidence.rules.yaml)使用三个**自定义指标契约**，均不是 CNPG/Barman/Ceph 内置指标。复用平台已有证据采集或 textfile collector；本仓库提供契约和告警，不部署采集服务。

| 指标及固定维度 | 允许推进时间的真实证据 | 告警起点 |
| --- | --- | --- |
| `dba_verified_recovery_point_timestamp_seconds{namespace="dba-a",cluster="pg-a",site="dr-a"}` | 独立站点可读的完整基础备份、连续 WAL/时间线与恢复标记，指向**数据可恢复到的时间** | 落后 >900s 持续 2min；整项缺失窗口 15min 后持续 2min |
| `dba_pitr_last_success_timestamp_seconds{namespace="dba-a",cluster="pg-a",site="test"}` | 新建隔离集群的 PITR 完成，目标前标记存在、目标后标记不存在且数据核对通过的完成时间 | >31 天持续 5min；缺失窗口 1h 后持续 5min |
| `dba_dr_last_restore_success_timestamp_seconds{namespace="dba-a",cluster="pg-a",site="dr-a"}` | 源站不可用条件下，在独立故障域完整恢复、核对数据及 RPO/RTO 通过的完成时间 | >100 天持续 5min；缺失窗口 1h 后持续 5min |

三项都以秒为单位的 Unix 时间持续暴露，每个预期 `namespace/cluster/site` 必须有自己的序列；其他站点、其他集群成功不能覆盖缺失。保存上次成功值，失败不能刷新成功时间，也不能在采集器重启、备份完成或抓取时改为当前时间。没有成功证据时暴露 `0` 并保持未通过；零值、负值、NaN、超出 60 秒时钟容差的未来值均有独立 critical 规则，不能等到月/季度窗口才发现无效数据。

采集端应持久化结果、原子更新证据，保留受控 run ID、验证人、备份 ID、timeline/LSN、标记、源/目标身份、实际 RPO/RTO 和日志引用；高基数证据 ID 不放入指标标签。输出周期应短于缺失窗口（示例 ≤60s），成功时间保持原值；采集失败不可伪造新结果。缺失窗口表示最近一段时间没有样本，再叠加规则 `for`，并非恰好该时间送达。人工检查如暂时代替自动采集，须明确责任人、检查周期与未自动化项；缺失告警和灾备未通过状态不能被静默抹去。

## 8. 指标、告警和 Grafana

[原生 Prometheus 规则](../examples/monitoring/cnpg-pg-a.rules.yaml)是针对一个示例集群的起点，要求实例抓取任务为 `job=cnpg-postgres`，并为每个实例注入 `namespace`、`cluster`、`pod`、`uid`；`cluster` 应来自 Pod 标签 `cnpg.io/cluster`。可将[抓取配置片段](../examples/monitoring/cnpg-pg-a.scrape.yaml)合并到既有 Prometheus；它使用 Kubernetes Pod 发现、实例 `metrics` 端口及 TLS。需给予 Prometheus 对对应 Namespace Pod 的只读 list/watch 权限，并在采集端挂载经过核验的**公开 CA 证书**，不得把包含 CA 私钥的完整 Secret 授予 Prometheus。默认 CNPG 服务证书的 `server_name` 示例为 `pg-a-rw`；实际证书 SAN、CA 轮换和访问规则须验证，严禁跳过 TLS 校验。每套集群的 CA/名称可以不同，应使用不同 scrape job 并同步修改规则的 `job` 标签；不要在同一 Prometheus 配置中重复定义相同 `job_name`。

Ceph OSD 性能任务示例为 `ceph-osd`，集群状态为 `ceph-cluster`，Operator 状态来自 `kube-state-metrics`。实际任务名、标签、Prometheus/CRD 能力和权限须以平台现状替换；CNPG 默认 SQL 指标有 `cnpg_pg_replication_*`、`cnpg_pg_replication_slots_pg_wal_lsn_diff`、`cnpg_pg_stat_archiver_*`，Barman 0.15.1 指标为 `barman_cloud_cloudnative_pg_io_*`。Ceph 版本中 OSD perf 可能来自独立 `ceph-exporter`；缺少 scrape 时应告警而不是假设为 0。

CNPG 1.30.1 的 `cnpg_collector_sync_replicas{value="observed"}` 从 `synchronous_standby_names` 解析**配置要求的数量**，不是当前活跃同步连接数。示例使用[自定义查询](../examples/monitoring/cnpg-pg-a.custom-queries.yaml)统计主库 `pg_stat_replication` 中 `state=streaming` 且 `sync_state` 为 `sync`/`quorum` 的连接，导出 `cnpg_dba_sync_eligible_replicas`；该功能在参考版为 beta，查询指定 `postgres` 数据库，须确认 `cnpg_metrics_exporter` 的 `pg_monitor` 与 `CONNECT` 权限、查询开销、指标存在和失联检测延迟。该计数依赖 PostgreSQL 对连接状态的观测，不能单独证明外部 fencing 或每笔事务已持久化。

### 8.1 Ready 数量与逐实例指标

`up=0` 只覆盖仍在发现中的抓取失败；整集群 `absent_over_time` 和“没有同步备库”均不能发现所有单实例丢失。规则现在分开检查：

- `dba_postgres_expected_instances` 是独立于实际 Pod/抓取数量的期望值，示例为 `3`，必须与批准的 `Cluster.spec.instances` 一致。扩缩容同一变更更新该值并检查实际 CR；有既有 CR 指标采集时可改由 spec 提供，不能从实际 Ready 数反推，不能为消除降级告警把 3 改为 2。
- `dba_postgres_instance_info` 从 **kube-state-metrics** 的 Pod 清单选择 `cnpg.io/cluster=pg-a`、`cnpg.io/podRole=instance`，排除正在删除的 Pod，保留 `namespace/pod/uid`，不从 CNPG scrape target 反推。实例初始化 Job 不计数；同名新 UID 不继承旧实例的指标。
- `dba_postgres_ready_instances` 以该清单关联 `kube_pod_status_ready{condition="true"}`，按 UID 去重后累计。低于期望持续 2min 为 warning；清单全部缺失会使计数保守显示 0，并触发独立的清单缺失 critical。此时 0 表示无法证实 Ready 数，不是已证明所有数据库都停机。
- `DbaPostgresInstanceMetricsMissing` 对每个现存实例执行 `unless on(namespace,cluster,pod,uid)` 检查 `cnpg_pg_replication_in_recovery`，缺失持续 2min 为 warning。若 Pod 被删除，改由期望数对 Ready 数检测冗余不足；即使主和另一同步备库正常也须报警。

平台已有 kube-state-metrics 必须允许 `pods=[cnpg.io/cluster,cnpg.io/podRole]`（合并到现有 `--metric-labels-allowlist`，不要覆盖其他条目），并采集 `kube_pod_labels`、`kube_pod_status_ready`、`kube_pod_deletion_timestamp`。KSM 会把 camelCase 标签转为小写 snake_case，`cnpg.io/podRole` 对应 `label_cnpg_io_pod_role`；Prometheus Pod 发现的元标签仍为 `__meta_kubernetes_pod_label_cnpg_io_podRole`，不能混用。核对 `label_cnpg_io_cluster` 和 `uid`；[KSM Pod 指标说明](https://github.com/kubernetes/kube-state-metrics/blob/main/docs/metrics/workload/pod-metrics.md)、[标签转换实现](https://github.com/kubernetes/kube-state-metrics/blob/v2.17.0/internal/store/utils.go)。升级时重新确认。清单/Ready 缺失不能按零需求处理；KSM/API 发现故障需从独立观测路径排查，KSM 缓存也不能证明旧主已停止。CNPG 抓取配置新增 Pod UID；迁移时先核验标签与规则，一次变更完成，不能长期忽略缺失告警。

### 8.2 业务写入探针契约

[业务与通知证据规则](../examples/monitoring/service-evidence.rules.yaml)要求从真实业务访问域、经实际 DNS/连接池/`rw` 路径，每 30s 执行一次受限事务；使用独立最小权限探测账号及专用小表，TLS 验证服务端身份，`synchronous_commit=on`。每次生成唯一幂等标识，提交并在新连接读回标记，核对数据库身份和实际后端；只有提交确认、读回及身份检查全部通过才算成功。连接和语句设超时，整次执行初始上限 5s，失败、超时或结果不确定均记为失败并保留 unknown 对账，不能无条件重试副作用。表的清理与保留单独限额，禁止使用真实业务表。

| 持续暴露的指标，固定 `namespace/cluster/probe="rw"` | 含义与规则 |
| --- | --- |
| `dba_postgres_write_probe_success` | 最近一次完整尝试结果，仅 0/1；失败持续 1min 为 critical |
| `dba_postgres_write_probe_last_attempt_timestamp_seconds` | 最近一次结束的尝试时间，无论成败；停止推进 >120s 持续 30s 为 critical，防止缓存 success=1 |
| `dba_postgres_write_probe_last_success_timestamp_seconds` | 最近通过的事务证据时间；从未成功为 0，>60s 无成功持续 30s 为 critical |

任一指标缺失窗口 2min 后持续 30s 为 critical；非法成功值、NaN、负时间或超过 60s 容差的未来时间另告警。探针应按 `namespace/cluster/probe` 提供单一逻辑结果，原子更新三个值，执行失败不能保留旧 success=1；运行与采集应彼此独立。多访问域/多站点需要不同 probe 名及对应缺失规则，不能让一个探针掩盖另一个。`ha_probe.py` 仍只用于隔离测试事务对账，**不是生产持续探针或常连接双写检测器**；生产探测及证据采集需接入平台现有设施并按本契约验收。

### 8.3 通知送达与监控自身

同一规则文件补齐 Prometheus 未发现 Alertmanager/发送错误/自指标缺失，以及 Alertmanager 渠道发送失败、配置重载失败、自指标缺失规则。任务名示例为 `prometheus` 和 `alertmanager`，需自抓取并在交接时按实际名称修改。失败计数以 5min 窗口、持续 1min 为起点；路由、重复间隔与 `for` 均计入真正通知时延。指标来源核对[Prometheus notifier](https://github.com/prometheus/prometheus/blob/main/notifier/metric.go)及[Alertmanager notify](https://github.com/prometheus/alertmanager/blob/main/notify/metrics.go)，现场版本须再检查。

`DbaNotificationWatchdog` 持续 firing，必须经**实际值班接收渠道**发送。合并以下子路由到现有 Alertmanager `route.routes` 的适当优先位置；`dba-oncall` 应是现有、已配置的真实接收方，不是空接收器。其余告警按既有 critical/warning 路由，配置恢复通知、重复发送及未确认升级；心跳不应被通用 severity 过滤、抑制或维护静默屏蔽：

```yaml
- matchers:
    - alertname="DbaNotificationWatchdog"
    - dba_cluster="pg-a"
  receiver: dba-oncall
  group_by: [alertname, dba_cluster]
  group_wait: 0s
  group_interval: 1m
  repeat_interval: 1m
```

最终接收端确认实际送达后，才更新 `dba_alert_notification_last_received_timestamp_seconds{namespace="dba-a",cluster="pg-a",receiver="dba-oncall"}`。保持每次回执的真实 Unix 秒并持续暴露；不能由 Prometheus 求值、Alertmanager 接受告警或中转 webhook 返回 200 刷新。超过 300s 未送达持续 1min、缺失窗口 5min 后持续 1min、零/负/NaN/超容差未来时间持续 1min 均有 critical 规则。无法取得最终送达回执时只能声明已验证的中间链路，保留人工终端收件核验与未自动化项，不能伪造送达指标。

最终接收端还必须在生产监控栈之外检查“每分钟心跳是否到达”，超时走**独立备用渠道**；不能只靠失效的 Alertmanager 给自己报警。每次验收保存：规则 pending/firing、Prometheus 到 Alertmanager、有效路由/静默/抑制、渠道送达 ID、值班终端实际收件时间、人员确认和升级、故障恢复通知。终端收到和人员确认分别记录；心跳送达不代替真实 critical/warning 及恢复消息的路由测试。

| 级别 | 初始触发与处置 |
| --- | --- |
| critical | 主数量非 1、实例/清单指标全缺、无同步备、业务写探针失败/停更/缺失、无效证据、归档失败、PVC <10%、基础备份超 26h/缺失、恢复点落后、OSD down、Operator 0 副本、通知链路失败或回执过期；核对真实写入和保护状态，暂停危险变更 |
| warning | Ready 少于期望/逐实例指标缺失持续 2min、备库回放 >30s/5min、接收器中断、槽积压、归档队列、PVC <20%/容量预测、Ceph 延迟、Operator 少于两副本、PITR/跨站演练逾期；按对应手册处置 |

阈值依赖示例 4GB 槽限制、每日备份和业务故障域，不构成通用 SLA。`cnpg_collector_pg_wal{value="size"}` 是 WAL 目录估算大小，`kubelet_volume_stats_*` 是 PVC 水位；CSI/kubelet 不提供时应接入已验证的文件系统监控并保留缺失告警。所有 `dba_*` 采集、通知路由和独立心跳均为交付前置项；仅加载规则或离线测试通过不代表已经实现采集、通知或恢复。

Grafana 优先复用平台已有数据源与权限，按 `namespace/cluster/pod` 展示主备、同步状态、复制时延/字节、槽距离、归档队列、PVC/WAL、备份时间、Ceph OSD 读写与恢复流量、Operator 可用副本；同屏叠加客户端提交延迟及维护事件。故障诊断时先从真实写探测和数据保护信号开始，再定位控制面、网络、存储或备份；告警的阈值、owner、通知路由和恢复条件须在演练中验证。

## 9. 多集群覆盖和实施顺序

每套集群以[部署文档的 Cluster 模板](deployment.md#5-创建一主两备集群)为共同基线，通过受控 Kustomize 覆盖环境差异；不共享实际凭据、PVC、对象前缀或资源配额。以下示例只覆盖 `Cluster`，在独立受控目录中放置已替换镜像等占位值的 `cluster.yaml`，再写 `kustomization.yaml` 并用 `kubectl kustomize` 渲染。`8GB` 和 `50Gi` 是第二套的示例，需按实际 WAL/存储预算重新核定：

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: dba-b
resources:
  - cluster.yaml
patches:
  - target:
      group: postgresql.cnpg.io
      version: v1
      kind: Cluster
      name: pg-a
    patch: |-
      - op: replace
        path: /metadata/name
        value: pg-b
      - op: replace
        path: /spec/storage/size
        value: 50Gi
      - op: replace
        path: /spec/postgresql/parameters/max_slot_wal_keep_size
        value: 8GB
      - op: replace
        path: /spec/plugins/0/parameters/barmanObjectName
        value: pg-b-backup
      - op: replace
        path: /spec/monitoring/customQueriesConfigMap/0/name
        value: pg-b-monitoring
```

不能只改 Cluster：第二套还必须分别覆盖 ObjectStore 名、唯一 S3 前缀/身份、ScheduledBackup 的 `cluster.name` 和时间、[Quota/LimitRange](../examples/namespace/pg-a-quota.yaml)、[NetworkPolicy](../examples/network/pg-a-networkpolicy.yaml)、[监控查询 ConfigMap](../examples/monitoring/cnpg-pg-a.custom-queries.yaml)中的名称/Namespace，以及[告警规则](../examples/monitoring/cnpg-pg-a.rules.yaml)、[恢复证据规则](../examples/monitoring/recovery-evidence.rules.yaml)和[业务/通知证据规则](../examples/monitoring/service-evidence.rules.yaml)中的 Namespace、集群、站点、阈值和任务标签。第二套三个 50 GiB PVC 至少申请 150 GiB，第一套的 100 GiB `requests.storage` 示例会阻止创建，必须连同重建及恢复余量重新设定；第二套 slot 预算为 8GB 时，示例 3 GiB 告警也不应原样复制。告警与容量交接一起调整，变更前逐项对照渲染结果、资源引用和目标身份，不要批量替换证据或凭据。

建议顺序：①交接版本、故障域、容量、独立对象存储和隔离方案；②审核并接入 Operator/CSI、网络和监控；③先建立一套测试集群，完成真实复制、ACK、PITR、Ceph 和告警验收；④按同一模板逐套覆盖并错开备份/维护；⑤执行[验收手册](acceptance.md)并登记未验证项。未通过任何关键保护项的集群不迁入生产写流量。
