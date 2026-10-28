# PostgreSQL 多集群生产验收与故障演练

本手册规定可重复的检查、观测、恢复和数据核对方式；真实故障注入仅在独立测试环境、经确认的目标和窗口内执行。已验证的静态配置或某一次历史实验不代表本手册的运行验收通过。

## 1. 统一准备与证据

1. 为每次演练记录 run ID、操作人、UTC 时间、源/恢复 Kubernetes UID、Ceph FSID、Namespace、Cluster UID、实例和 PVC/RBD 归属、版本及镜像 digest。禁止将真实地址、身份材料、凭据或业务数据写入公开仓库。
2. 使用有独立测试数据的集群、对象前缀和客户端；与生产控制面、Ceph、网络、供电隔离。核验三实例、独立卷、同步策略、`failoverQuorum` 有效状态、旧主隔离、证书、告警与可恢复备份；对跨站演练另核验恢复端故障域。
3. 每个注入必须有明确目标、单次/并发范围、开始和最大持续时间、停止阈值、独立回退通道和现场负责人。记录注入前后的资源状态；只处理本次对象，禁止重置集群、清空 OSD、强拆未知卷或手工删除 WAL。
4. 控制面不足仲裁、数据身份不符、备份不完整、隔离不可证明、节点容量不足、磁盘临近保护阈值或回退路径失效时，停止新增注入并先恢复环境；未完成的项目标为未通过或未执行。
5. 故障事件、监控、客户端和数据库使用可核验的时间同步；记录故障开始、探测、旧主隔离、Lease/选举、目标晋升、Service 端点、客户端首个成功提交和全部恢复时间。将确定成功的事务、结果未知的事务分开。

每项证据至少包含：初始状态、注入对象和方式、指标/事件/日志、已确认事务清单和恢复数据核对、耗时、回退结果、结论及残留风险。日志脱敏并保存在受控位置；一个场景失败不能用恢复后健康或其他场景成功覆盖。

## 2. 事务确认与对账工具

仓库的 [`ha_probe.py`](../../scripts/ha_probe.py) 仅用于**隔离测试数据库**。它逐条使用 `synchronous_commit=on`、单独事务和唯一 `run_id/seq`；只有 psql 成功返回才记为 `ack`，连接失败或超时记为 `unknown`。证据以独占创建的 0600 TSV 写入仓库外，并逐条 flush/fsync；程序中断或证据缺行时不允许将对账标为通过。该工具不能证明业务所有会话都同步，也不能替代旧主 fencing、物理持久化或完整业务数据对账。

在隔离测试库中由授权的数据库 owner 创建专用表，并按需向测试身份授予 `INSERT` 与 `SELECT`；不得指向真实业务表。审查表及触发器，避免外部副作用：

```sql
CREATE TABLE public.dba_ha_probe (
    run_id uuid NOT NULL,
    seq bigint NOT NULL,
    committed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, seq)
);
```

在仓库根目录，从获准的测试客户端设置 `PGHOST` 为写 Service 的证书 DNS、`PGPORT`、`PGDATABASE`、`PGUSER`、`PGPASSFILE`、`PGSSLROOTCERT` 和 `PGSSLMODE=verify-full`；密码文件只能由当前用户访问，禁止使用 `PGPASSWORD` 或 `PGSERVICE`。先按[部署文档](deployment.md#61-主备卷与-service)核对 Kubernetes UID、集群名、同步状态和卷；脚本还会核对数据库的 `cluster_name`，但同名集群可能存在于其他 Namespace，`--test-only` 和名称检查不能自动证明目标不是生产环境。

在一个测试终端运行写入，在另一个终端实施已登记的故障；设置足够的条数覆盖故障前后，确保故障前和恢复后各有至少一笔 `ack`。以下路径须替换为受控的仓库外证据目录：

```bash
export EVIDENCE='<受控证据目录>/ha-run.tsv'
python3 scripts/ha_probe.py write --test-only --expected-db app --expected-cluster '<测试集群名>' \
  --evidence "$EVIDENCE" --count 300 --interval 1 --timeout 5
```

在注入终端设置相同的 `EVIDENCE` 路径，以下只读检查成功才表示故障前至少已有一条已落盘的 ACK；记录注入时间，不能把进程仍在运行当作已有成功提交：

```bash
awk -F '\t' 'NR > 1 && $6 == "ack" { found = 1 } END { exit !found }' "$EVIDENCE"
date -u +'%Y-%m-%dT%H:%M:%SZ'
```

故障结束、客户端恢复且所有记录完成后，将客户端连接目标明确指向已确认的新主，再执行：

```bash
python3 scripts/ha_probe.py verify --expected-db app --expected-cluster '<测试集群名>' --evidence "$EVIDENCE"
```

必须 `missing_ack=0`、无多余序号、证据完整，且故障前后均有确认事务。`unknown_present`/`unknown_absent` 是连接失败时事务结果的分类，不能一律算作丢失或成功；应用须按幂等键对账。按 `finished_utc` 中故障后的首条 `ack` 计算客户端写恢复时间；工具只核对专用表，不代表其他业务表、外部系统或整站备份已验证。

PITR 使用另一组测试 `run_id`，且不能把目标时间之后的已确认事务判为恢复丢失。确认基础备份已完成后，在**测试主库**依次执行以下步骤；将 UUID 替换为本次独立值，在两次提交之间保存数据库返回的 UTC 时间作为 `targetTime`：

```sql
BEGIN;
SET LOCAL synchronous_commit = 'on';
INSERT INTO public.dba_ha_probe(run_id, seq) VALUES ('<TEST_RUN_UUID>', 1);
COMMIT;
SELECT to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS target_time;
BEGIN;
SET LOCAL synchronous_commit = 'on';
INSERT INTO public.dba_ha_probe(run_id, seq) VALUES ('<TEST_RUN_UUID>', 2);
COMMIT;
```

记录两次提交成功、时间源和 UUID；等待或在授权测试环境关闭目标后 WAL 段，并核验该时间线的完整归档及独立副本可读取。恢复到保存的 `targetTime` 后，查询 `SELECT seq FROM public.dba_ha_probe WHERE run_id = '<TEST_RUN_UUID>' ORDER BY seq;`：应只有 `1`。基础备份和 WAL 未覆盖目标时、未找到目标后 WAL 时、或时间证据不可靠时停止，不能宣称 PITR 成功。

## 3. 通用指标与验收边界

所有场景同时观察：实际客户端写入成功率和事务延迟、ACK/unknown、主备/Lease/端点、同步确认数及 LSN、WAL/槽/归档、PVC 空间、Ceph HEALTH/PG/OSD 与延迟、Operator 和 API 状态、告警通知。指标名与告警阈值见[运维手册](operations.md#8-指标告警和-grafana)。

单主故障的待验证目标是 5 分钟内客户端恢复提交，并在同步、仲裁、旧主隔离和持久化前提成立的场景中不丢失已确认事务。复合故障、仲裁不足、整站恢复和存储全局故障不默认适用 5 分钟目标；每项记录实际 RTO/RPO，未知则标记未验证。

## 4. 场景、步骤与完成标准

每行的“注入”均指测试环境中按第 1 节受控执行的动作；网络/主机/Ceph 的实现由实际平台的故障注入与独立回退机制承担，不能凭本文猜测网卡、OSD 或生产地址。每个动作完成后先撤销注入，复核原有策略及所有健康条件，再执行下一项。

| 场景 | 执行与观测 | 通过标准及数据核对 |
| --- | --- | --- |
| 主 Pod 故障 | 用[部署文档的 UID 前置条件示例](deployment.md#81-预期自动切换与受控演练)删除隔离测试主 Pod；观察旧主退出、Lease、仲裁、新主和端点 | 两备节点可仲裁、旧主停止写、唯一新主；客户端 5 分钟内成功提交；工具对账 `missing_ack=0` |
| 单备节点故障 | 在隔离环境停止一个已核验备 Pod/节点；确认另一同步备及写主存活 | 客户端可继续同步提交；记录写延迟和降级告警，恢复后同步副本及三实例状态正常，ACK 不丢 |
| 主和一备同时不可用 | 确保注入是同一故障窗口，记录当时 `FailoverQuorum` 的 R/W/N 和两个故障对象；不手动晋升 | 示例中剩余一个候选时自动晋升被阻止；无未经隔离的第二写主，恢复候选/人工决策后核对 ACK；不把等待计为 5 分钟通过 |
| 主节点与 API、其他实例分区但仍可被客户端访问 | 使用有独立撤销期限的测试网络隔离；同时测试旧主现有连接、双方 8000/5432、API、Lease 和候选状态 | 在批准的隔离机制生效前不能出现两个可写主；若仍可双写或无法证实旧主停止，停止生产准入；逐笔核对 ACK/unknown |
| Kubernetes API 不可达 | 在测试网络中阻断 Operator/实例到 API，保留可独立撤销的回退路径；观察实例间连通性、Lease 和客户端 | 不声称可以安全选主或保证持续写；API 恢复后检查唯一主、同步、Service、证据和 ACK，测量控制面与写入分别恢复时间 |
| Operator leader 故障 | 检查两 Operator 副本、leader Lease 和资源；仅在测试集群停止当前 Operator Pod | 另一副本取得 leader，Webhook/控制链恢复；数据库真实写探测和 ACK 符合目标；无双 Operator leader，记录恢复时间 |
| Ceph OSD 故障及重平衡 | 在独立测试 Ceph 中按存储负责人的故障计划隔离一个 OSD；观察 HEALTH/PG、OSD、恢复流量、客户端提交和 PVC | 在批准的池副本/容量边界内，已确认事务不丢；记录数据库 P95/P99、恢复期间性能和 Ceph 最终状态；不改 `min_size`/强清数据 |
| 槽积压与失效 | 使用专用测试备节点或临时测试消费者，中断该消费链并保持归档正常；记录 `restart_lsn`、`safe_wal_size`、`wal_status`、WAL 和空间；在约定水位前恢复 | 告警在阈值出现并送达；槽未失效时追上；若在独立测试中有意触发 `lost`，按运行手册验证归档补齐或安全重建、ACK 不丢；不得误删 CNPG 槽 |
| WAL 归档中断 | 临时阻断测试对象端点，保留 DNS、数据库复制和独立回退；观察 `.ready`、失败时间、WAL 目录及 PVC 预测 | 归档告警有效，槽上限不被误认为硬磁盘上限；在空间停止阈值前恢复端点，连续 WAL 到达并验证恢复链，ACK 不丢 |
| 基础备份失败/恢复 | 在测试对象前缀中限制测试写身份或中断测试端点；观察 Backup 状态、插件、告警和对象 | 失败与成功状态分开记录；恢复凭据后重新完成备份并校验 WAL，单靠 `completed` 不算 PITR 通过 |
| PITR | 完成独立基础备份；先提交标记 A，记录带时区目标，再提交标记 B；确认 WAL 已归档，按[部署文档](deployment.md#72-在隔离环境执行-pitr)恢复新集群 | A 存在、B 不存在，基线数据/哈希和扩展一致；记录恢复点与时间，失败不得覆盖源数据 |
| 跨站恢复 | 源站隔离或不可达条件下，从独立副本及凭据在独立 Kubernetes/存储环境恢复；确认整个 WAL/时间线与镜像可读 | 核对基础数据、PITR 标记、业务约束及实际 RPO/RTO；仅在另一 Namespace 恢复不能算跨站通过 |
| Operator 与 PostgreSQL 更新 | 在两套测试集群上先后演练 Operator 更新、小版本更新与 supervised 主切换；记录滚动开始时间、同步与故障 | 逐集群验证备份、主备、客户端和 ACK；延迟参数仅限制发起间隔，不能用其代替前一集群 Ready 检查；大版本使用独立迁移验收 |

## 5. 结果归档与回退

每次试验应在回退后再次核对：不需要业务接受的旧主写路径关闭、所有测试网络隔离规则撤销、API/Operator 和 Ceph 恢复、备节点追平、归档持续、备份链可恢复、告警已恢复，并登记保留的测试 PVC/RBD 和对象。清理不得自动删除 Retain 卷、未知快照或共享备份；如现场状态未恢复，保留失败与具体残留项并停止后续演练。
