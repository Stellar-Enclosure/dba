# PostgreSQL 多集群部署、验收与故障处置

| 项目 | 内容 |
| --- | --- |
| 编制日期 | 2026-10-28 |
| 部署对象 | 已交接的 Kubernetes、独立 Ceph 和对象备份环境 |
| 适用路径 | CloudNativePG、PostgreSQL 一主两备、Ceph-CSI RBD、Barman Cloud Plugin |
| 关联设计 | [运维架构设计](architecture.md) |
| 状态 | 操作模板；环境参数、物料和验收结果均须在实际部署时记录 |

本文件不代表目标环境已经部署或通过验收。示例是新建空集群，不接管或覆盖现有 PostgreSQL、PVC、对象前缀、Ceph 池和 Operator。所有实际操作在对应环境的变更窗口内由授权人员执行；任何前置核对不符时停止并保留现场。

## 1. 交接清单与停止条件

| 交接项 | 执行前必须确定 |
| --- | --- |
| 目标身份 | kubeconfig、context、API 身份、`kube-system` UID、环境名称和允许创建的 Namespace；Ceph FSID 和池由存储负责人独立核对 |
| 组件版本 | Kubernetes、CloudNativePG Operator/CRD、`kubectl cnpg`、PostgreSQL/CNPG 镜像、Barman 插件、cert-manager、Ceph/CSI、客户端和扩展的兼容矩阵 |
| 物料 | 固定版本的 Operator 和插件清单或 Chart、镜像 digest、CA、内部镜像仓库和离线恢复副本；核对来源、摘要和发行说明；管理端具备 kubectl、jq 和受控的 PostgreSQL 客户端 |
| 故障域 | 至少三个符合反亲和的节点；若要求单节点故障后重建至三实例，则至少四个；物理宿主机、存储、网络和供电映射按目标验收 |
| 资源 | 每实例 CPU/内存/连接数、数据和 WAL 高峰、PVC 容量、Ceph 池/物理余量、备份/恢复额外资源及故障后容量 |
| 存储 | 已验收的 RBD StorageClass、`Retain`、扩容、CSI 节点覆盖、卷独占与旧写入者隔离；不得创建第二套竞争同一设备的 CSI |
| 安全和网络 | 最小权限账号、密钥交付与轮换、客户端证书 DNS、Operator/插件证书、访问路径及网络策略正反向测试 |
| 备份 | 独立故障域对象存储、唯一空前缀、TLS CA、受限写身份、恢复只读身份、保留周期和恢复环境 |
| 可用性 | 自动切换、旧主隔离、仲裁不足、应用会话同步提交、PITR、告警和故障演练的可验证验收标准 |

示例参考 CloudNativePG **1.30.1**、PostgreSQL **18.6** 和 Barman Cloud Plugin **0.15.1** 的接口；CNPG 1.30 文档的支持矩阵包括 Kubernetes 1.34–1.36 和 PostgreSQL 14–18。它们是本文编写时的参考组合，不是永久版本锁：实施时必须重新确认版本支持、安全修复、实际镜像及插件兼容，固定补丁版本和 digest。[CNPG 1.30 发行说明](https://cloudnative-pg.io/docs/1.30/release_notes/v1.30/)、[Barman 插件安装](https://cloudnative-pg.io/plugin-barman-cloud/docs/installation/)

示例采用同步副本数 1、`dataDurability: required` 和 `failoverQuorum: true`。后者在所参考文档中仍属实验性，必须核实版本和生产接受条件。**若旧主隔离、仲裁、应用会话提交语义或独立恢复无法通过实际验证，不得将示例接入需要该保障的生产业务。** 不能假定 `primaryUpdateStrategy: supervised` 或人工值守会阻止故障自动切换；前者只影响受管更新。

以下示例使用 `dba-a` / `pg-a` / `app`，所有示例域名、仓库和桶名称均为占位值；替换为已登记的实际值后再执行。第二套集群独立使用 `dba-b` / `pg-b`、独立凭据和对象前缀，按同一流程逐套执行，不共享数据库数据目录或归档目录。示例文件保存在仓库外受控目录，不能放入真实密码或密钥。

## 2. 核对目标环境

下例在专用 Bash shell 中依次执行；`k` 始终固定 context，`set -euo pipefail` 令未处理的错误停止该 shell。`EXPECTED_KUBE_SYSTEM_UID` 必须来自独立交接记录，不能用本次查询结果自行赋值。示例所需 Namespace 应为本次新建，若同名对象已存在则停止确认归属，不执行覆盖。

```bash
set -euo pipefail
export KUBE_CONTEXT='<交接的 context>'
export EXPECTED_KUBE_SYSTEM_UID='<交接的 kube-system UID>'
export NS='dba-a'
export CLUSTER='pg-a'
export CNPG_NS='cnpg-system'

test -n "$KUBE_CONTEXT" && test -n "$EXPECTED_KUBE_SYSTEM_UID"
k() { kubectl --context "$KUBE_CONTEXT" "$@"; }
kubectl config get-contexts "$KUBE_CONTEXT"
k version
actual_uid="$(k get namespace kube-system -o jsonpath='{.metadata.uid}')"
test "$actual_uid" = "$EXPECTED_KUBE_SYSTEM_UID" || { echo '目标集群身份不符'; exit 1; }
k get nodes -o wide
k get storageclass
if ! k get crd clusters.postgresql.cnpg.io objectstores.barmancloud.cnpg.io; then
  echo 'CRD 查询失败；区分对象不存在、权限不足和 API 不可达后再处理'
fi
```

如 CRD 尚未安装，最后一条查询会报不存在；记录现状后进入第 3 节。若同时存在其他 CNPG 版本、同名 CRD、Webhook、Operator 或 Barman 插件，先确认现有管理者和兼容性，禁止以本示例覆盖或并行争用。查询 Namespace、集群名、PVC、备份前缀和既有 Secret 是否冲突；发现任何已有数据时停止，改走独立的接管或迁移设计。

检查目标 StorageClass 的 `provisioner`、`reclaimPolicy`、`allowVolumeExpansion`、`volumeBindingMode` 与后端池，确认 `Retain` 和独占卷行为已验证。存储负责人在**已核验身份的 Ceph 管理端**核对集群健康、MON/OSD、池副本及容量，不通过 kubeconfig 猜测 Ceph 身份：

```bash
ceph fsid
ceph health detail
ceph osd tree
ceph df detail
ceph osd pool ls detail
```

将 FSID 与交接值人工比较；池归属、副本数、`min_size`、CRUSH 故障域及可用空间均需满足平台批准基线。任何容量不足、健康异常、物理故障域不明或 CSI 认证/挂载未验收均阻断生产部署。节点标签需由平台事先设置并核实：示例使用 `dba.example/eligible=true` 和节点 `kubernetes.io/hostname`；实际物理隔离需替换成经过验证的拓扑键，不能只给节点改标签就宣称已隔离。

## 3. 安装或接入共享组件

### 3.1 Operator

由平台负责人先判断是**接入现有兼容 Operator**还是**首次安装**。若已存在，不执行下面的首次安装命令；按现有管理方式确认版本、监听范围、CRD、Webhook、Lease 权限、镜像和升级边界。本示例不指导覆盖或升级现有 Operator。

首次安装时，从 [CNPG 发行清单](https://cloudnative-pg.io/docs/1.30/installation_upgrade/)取得与选定版本匹配的完整清单，保存到受控目录；核对发布源、摘要、镜像架构和 digest，并检查 Namespace、RBAC、CRD、Webhook、`coordination.k8s.io/leases` 权限以及镜像引用。变量应指向已审核的**本地完整清单**，不是未经校验的远程 URL。

```bash
export CNPG_MANIFEST='<受控目录中的 CNPG 清单>'
test -r "$CNPG_MANIFEST"
k create --dry-run=server -f "$CNPG_MANIFEST"
k create -f "$CNPG_MANIFEST"
k -n "$CNPG_NS" rollout status deployment/cnpg-controller-manager --timeout=5m
k get crd clusters.postgresql.cnpg.io backups.postgresql.cnpg.io scheduledbackups.postgresql.cnpg.io
```

仅在全部相关资源确认不存在时使用 `create`；安装中途失败时先核对已创建对象的归属和状态，不能整组重跑或删除共享资源。若安装清单包含当前不存在的 Namespace，server dry-run 不会持久化 Namespace，依赖它的资源可能报错；先由平台按清单创建/确认 Namespace，再重新校验。共享 Operator 的变更会影响多套数据库，升级和回退需独立评估。

### 3.2 Barman Cloud Plugin

插件与 Operator 安装在同一管理 Namespace。先核对 cert-manager 或已交接证书，以及插件服务、CRD、sidecar 镜像和对象存储网络；插件部署文件中的镜像也须固定。以下仅用于首次安装且清单已按目标 Namespace 审核的情况；实际 Deployment 名以清单为准。

```bash
export BARMAN_MANIFEST='<受控目录中的 Barman 插件清单>'
test -r "$BARMAN_MANIFEST"
k create --dry-run=server -f "$BARMAN_MANIFEST"
k create -f "$BARMAN_MANIFEST"
k -n "$CNPG_NS" rollout status deployment/barman-cloud --timeout=5m
k get crd objectstores.barmancloud.cnpg.io
```

核对插件所需 Certificate、Secret、Service 和证书续期正常；若 CertificateRequest 需要平台审批，按既有受限流程处理。失败时不要关闭 TLS 验证、放宽全部网络策略或用未核定的旧式备份字段代替插件。

## 4. 为第一套集群准备身份与对象存储

只在确认 Namespace 不存在时新建；先应用平台的 PSA、ResourceQuota、LimitRange、RBAC 和网络策略。测试时必须允许第 7.3 节列出的实际连接路径，同时验证未授权访问被拒绝。节点上所需的 CSI 和镜像已由平台交付。

```bash
k create namespace "$NS"
k -n "$NS" get resourcequota,limitrange,networkpolicy
```

凭据文件由安全交接产生，保存在受限目录。下面的 `APP_PASSWORD_FILE` 是无尾随换行的密码字节文件，S3 身份文件亦不得含意外换行；密钥内容不要作为命令行参数、写入终端输出或提交 Git。`backup-s3-ca.pem` 为目标服务的可信 CA，不是私钥。确认 Secret 名不存在后创建，后续轮换使用单独的轮换流程。

```bash
export APP_PASSWORD_FILE='<仓库外应用密码文件>'
export S3_ACCESS_KEY_FILE='<仓库外 S3 access key 文件>'
export S3_SECRET_KEY_FILE='<仓库外 S3 secret key 文件>'
export S3_CA_FILE='<仓库外 S3 服务 CA 文件>'
test -s "$APP_PASSWORD_FILE" && test -s "$S3_ACCESS_KEY_FILE" && test -s "$S3_SECRET_KEY_FILE" && test -s "$S3_CA_FILE"
k -n "$NS" create secret generic pg-app-auth --type=kubernetes.io/basic-auth \
  --from-literal=username=app --from-file=password="$APP_PASSWORD_FILE"
k -n "$NS" create secret generic backup-s3-credentials \
  --from-file=ACCESS_KEY_ID="$S3_ACCESS_KEY_FILE" --from-file=ACCESS_SECRET_KEY="$S3_SECRET_KEY_FILE"
k -n "$NS" create secret generic backup-s3-ca --from-file=ca.crt="$S3_CA_FILE"
```

确认密钥来源和传输方法不会通过终端录屏、进程命令行、CI 日志或备份材料泄露。确认对象身份只能访问该集群被允许的前缀；源写入身份与灾备恢复只读身份分开。将下面内容保存为受控目录 `objectstore.yaml`，替换占位路径和 HTTPS 端点，确认该前缀为空、属于本集群，7 天只是示例保留值：

```yaml
apiVersion: barmancloud.cnpg.io/v1
kind: ObjectStore
metadata:
  name: pg-a-backup
  namespace: dba-a
spec:
  retentionPolicy: 7d
  configuration:
    destinationPath: s3://replace-bucket/replace-unique-pg-a-prefix
    endpointURL: https://object-store.example.invalid
    endpointCA:
      name: backup-s3-ca
      key: ca.crt
    s3Credentials:
      accessKeyId:
        name: backup-s3-credentials
        key: ACCESS_KEY_ID
      secretAccessKey:
        name: backup-s3-credentials
        key: ACCESS_SECRET_KEY
    wal:
      compression: gzip
    data:
      compression: gzip
```

```bash
export OBJECTSTORE_FILE='<受控目录中的 objectstore.yaml>'
k create --dry-run=server --validate=strict -f "$OBJECTSTORE_FILE"
k create -f "$OBJECTSTORE_FILE"
k -n "$NS" get objectstore pg-a-backup
```

确认目标存储及授权路径属于独立故障域。S3 兼容实现的 multipart、对象列举、删除、TLS 和插件恢复能力需按锁定版本实测；只看到 ObjectStore 已创建不代表备份已成功。

## 5. 创建一主两备集群

将以下示例保存为 `cluster.yaml`。替换 Namespace、集群名、已核验镜像 digest、StorageClass、节点标签和资源值。示例 20 GiB、2 GiB 内存请求等只是占位，需依据第 1 节容量交接重新测算。`kubernetes.io/hostname` 仅表示节点域；若要求物理宿主域，替换为平台交接并核验过的标签。

```yaml
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: pg-a
  namespace: dba-a
spec:
  instances: 3
  imageName: <CNPG_POSTGRESQL_18_IMAGE_BY_DIGEST>
  enableSuperuserAccess: false
  primaryUpdateStrategy: supervised
  smartShutdownTimeout: 0
  bootstrap:
    initdb:
      database: app
      owner: app
      secret:
        name: pg-app-auth
  postgresql:
    parameters:
      synchronous_commit: 'on'
      archive_timeout: 5min
    synchronous:
      method: any
      number: 1
      dataDurability: required
      failoverQuorum: true
    pg_hba:
      - hostnossl all all all reject
      - hostssl app app all scram-sha-256
      - hostssl all all all reject
  storage:
    storageClass: <VERIFIED_RBD_RETAIN_STORAGECLASS>
    size: 20Gi
  affinity:
    enablePodAntiAffinity: true
    podAntiAffinityType: required
    topologyKey: kubernetes.io/hostname
    nodeSelector:
      dba.example/eligible: 'true'
  resources:
    requests:
      cpu: '1'
      memory: 2Gi
    limits:
      memory: 4Gi
  plugins:
    - name: barman-cloud.cloudnative-pg.io
      isWALArchiver: true
      parameters:
        barmanObjectName: pg-a-backup
```

用户定义 HBA 规则位于 CNPG 固定的本地/复制/连接池规则之后、默认规则之前。示例只允许 `app` 角色经 TLS 访问 `app` 数据库；增加业务角色、监控身份或连接池时必须先调整并验证规则。`hostssl ... all` 不限制网络来源，仍须由经验证的网络策略和授权来源限制访问。示例设置 `smartShutdownTimeout: 0` 缩小探测触发后的旧连接窗口，但不构成外部 fencing。

```bash
export CLUSTER_FILE='<受控目录中的 cluster.yaml>'
k create --dry-run=server --validate=strict -f "$CLUSTER_FILE"
k create -f "$CLUSTER_FILE"
k -n "$NS" get cluster "$CLUSTER" -w
```

上述 `-w` 是操作者在实施时主动运行的观察命令，满足条件后自行退出。等待三实例 Ready，检查事件和实际 Pod 分布；Pending 时先检查资源、拓扑、CSI、镜像和证书，禁止自动放宽反亲和或删除 PVC。

## 6. 创建后核对与多集群扩展

### 6.1 主备、卷与 Service

```bash
k -n "$NS" get cluster "$CLUSTER" -o wide
k -n "$NS" get pods -l "cnpg.io/cluster=$CLUSTER" -o wide
k -n "$NS" get pvc
k -n "$NS" get service "${CLUSTER}-rw" "${CLUSTER}-ro" "${CLUSTER}-r"
k -n "$NS" get endpointslice -l "kubernetes.io/service-name=${CLUSTER}-rw" -o wide
k -n "$NS" get endpointslice -l "kubernetes.io/service-name=${CLUSTER}-ro" -o wide
k -n "$NS" get lease "$CLUSTER"
k -n "$NS" describe cluster "$CLUSTER"
```

核对三个 Pod 分别位于指定的三个节点故障域，三个数据 PVC 分别绑定到不同 RBD image，`rw` 恰有一个 Ready 主端点，`ro` 仅有 Ready 备端点。还需对照 Namespace、Owner UID、StorageClass、PV 回收策略、Ceph 卷归属、镜像实际 digest、Lease、集群条件与告警；存在额外 Ready 写主或身份对不上时停止接入业务。

从 Kubernetes 状态获取主 Pod 后，在主实例内执行只读查询。SQL 中的 `synchronous_commit` 仅是当前诊断会话值，不能代替业务每个会话的验证。

```bash
PRIMARY="$(k -n "$NS" get cluster "$CLUSTER" -o jsonpath='{.status.currentPrimary}')"
test -n "$PRIMARY"
k -n "$NS" exec "$PRIMARY" -c postgres -- psql -X -U postgres -d postgres -v ON_ERROR_STOP=1 \
  -c 'SHOW synchronous_commit' -c 'SHOW synchronous_standby_names' \
  -c 'SELECT application_name, state, sync_state, sent_lsn, write_lsn, flush_lsn, replay_lsn FROM pg_stat_replication' \
  -c 'SELECT slot_name, active, restart_lsn FROM pg_replication_slots' \
  -c 'SELECT archived_count, last_archived_wal, last_archived_time, failed_count, last_failed_wal, last_failed_time FROM pg_stat_archiver'
```

确认复制连接正常、至少一个可用同步备节点和预期的同步名称；再分别在真实应用连接路径中核查提交设置和读写行为。普通用户可自行改变会话或事务的 `synchronous_commit`，仅设默认值、角色默认值或撤销参数授权不能强制所有事务同步；必须通过应用约束和验证支撑一致性目标。

从获准的客户端环境使用 `sslmode=verify-full`、可信 CA、实际匹配证书的 Service DNS 和受控密码文件建立连接。验证写入只能走 `rw`；允许延迟的查询走 `ro`。执行无效密码、非 TLS、错误 CA/主机名、未授权来源和只读入口写入的负向测试。受限 Pod 内连接如需使用 DNS，应使用证书 SAN 覆盖的完整域名；不要用 IP 或 `sslmode=require` 冒充身份校验。

### 6.2 建立第二套及后续集群

为每套集群重复第 2、4、5、6 节：重新确定目标身份和容量，使用不同 Namespace/集群名、独立 Secret、唯一 S3 前缀和备份身份。修改示例的 `metadata.namespace`、`metadata.name`、Secret 和 ObjectStore 引用后做 server dry-run；禁止将同一套集群的 PVC、Secret 或归档路径作为新集群初始化输入。每套都需要独立的 Service/TLS、容量、备份与恢复验证，不能仅凭第一套成功标记全部成功。

## 7. 备份、恢复与持续运维

### 7.1 立即备份与周期备份

本例使用插件方式；按实际集群名保存 `backup.yaml`，每次手工备份使用新名称。`completed` 只是基础备份完成，仍要核对归档及恢复。

```yaml
apiVersion: postgresql.cnpg.io/v1
kind: Backup
metadata:
  name: pg-a-first-backup
  namespace: dba-a
spec:
  cluster:
    name: pg-a
  method: plugin
  pluginConfiguration:
    name: barman-cloud.cloudnative-pg.io
```

```bash
export BACKUP_FILE='<受控目录中的 backup.yaml>'
k create --dry-run=server --validate=strict -f "$BACKUP_FILE"
k create -f "$BACKUP_FILE"
k -n "$NS" wait --for=jsonpath='{.status.phase}'=completed backup/pg-a-first-backup --timeout=60m
k -n "$NS" describe backup pg-a-first-backup
```

核对插件 sidecar/备份状态、对象存储可读性、实际归档与失败告警；不能根据 `last_archived_wal` 的文件名字典序跨时间线推断 WAL 完整。目标站点必须确认基础备份、连续 WAL、时间线和元数据均已复制且可读取；最终以恢复结果为准。

保存周期备份示例为 `scheduled-backup.yaml`：

```yaml
apiVersion: postgresql.cnpg.io/v1
kind: ScheduledBackup
metadata:
  name: pg-a-daily
  namespace: dba-a
spec:
  schedule: '0 0 2 * * *'
  backupOwnerReference: self
  cluster:
    name: pg-a
  method: plugin
  pluginConfiguration:
    name: barman-cloud.cloudnative-pg.io
```

这是包含**秒**字段的六位 cron 表达式；示例为每天 02:00，实施时必须确认 Operator 时区及实际首次触发时间。备份窗口、频率和保留期由业务与存储容量确认；完成首次独立恢复验证后再按实际计划投入周期运行。

```bash
export SCHEDULE_FILE='<受控目录中的 scheduled-backup.yaml>'
k create --dry-run=server --validate=strict -f "$SCHEDULE_FILE"
k create -f "$SCHEDULE_FILE"
k -n "$NS" get scheduledbackup,backup
```

删除 `ScheduledBackup`、调整保留期或删除对象前，先确认所有恢复链和共享关系；禁止把 Ceph 快照、PV Retain 或 `Backup completed` 当作独立的恢复证明。

### 7.2 在隔离环境执行 PITR

灾难恢复演练须在已交接的**独立 Kubernetes 和存储故障域**验证；如果仅在源 Kubernetes 内使用隔离 Namespace 做 PITR，只能证明该环境内的备份读取和时间点恢复，不能宣称整站灾难恢复通过。恢复端的 Operator、插件、证书、网络和 StorageClass 必须按第 1–3 节预先核验。下面的 `r` 与源环境的 `k` 分开，恢复端 UID 必须从独立交接记录取得：

```bash
export RESTORE_KUBE_CONTEXT='<交接的恢复环境 context>'
export EXPECTED_RESTORE_KUBE_SYSTEM_UID='<交接的恢复环境 kube-system UID>'
export RESTORE_NS='dba-restore'
r() { kubectl --context "$RESTORE_KUBE_CONTEXT" "$@"; }
kubectl config get-contexts "$RESTORE_KUBE_CONTEXT"
restore_uid="$(r get namespace kube-system -o jsonpath='{.metadata.uid}')"
test "$restore_uid" = "$EXPECTED_RESTORE_KUBE_SYSTEM_UID" || { echo '恢复环境身份不符'; exit 1; }
r get crd clusters.postgresql.cnpg.io objectstores.barmancloud.cnpg.io
```

确认目标 `dba-restore` Namespace、`pg-a-restore` 集群和 PVC 均不存在；该 Namespace 的网络只允许恢复和验证人员访问，不连接原业务 Service。确认受支持的同 major 镜像、恢复端容量、源集群名 `pg-a`、源 ObjectStore 路径、有效的备份和完整 WAL，以及目标时区时间。仅在确认 Namespace 不存在时创建它并配置平台 RBAC、配额与网络策略；恢复对象 Secret 和 CA 在恢复 Namespace 中单独创建，使用**只读**对象身份，不复制源写凭据。

```bash
export RESTORE_S3_ACCESS_KEY_FILE='<仓库外恢复只读 access key 文件>'
export RESTORE_S3_SECRET_KEY_FILE='<仓库外恢复只读 secret key 文件>'
export RESTORE_S3_CA_FILE='<仓库外恢复对象服务 CA 文件>'
test -s "$RESTORE_S3_ACCESS_KEY_FILE" && test -s "$RESTORE_S3_SECRET_KEY_FILE" && test -s "$RESTORE_S3_CA_FILE"
r create namespace "$RESTORE_NS"
r -n "$RESTORE_NS" create secret generic restore-s3-reader \
  --from-file=ACCESS_KEY_ID="$RESTORE_S3_ACCESS_KEY_FILE" --from-file=ACCESS_SECRET_KEY="$RESTORE_S3_SECRET_KEY_FILE"
r -n "$RESTORE_NS" create secret generic restore-s3-ca --from-file=ca.crt="$RESTORE_S3_CA_FILE"
```

以下保存为 `restore-objectstore.yaml`，替换为经验证的独立副本的桶、前缀和 HTTPS 端点；确保复制后的路径及 `serverName` 与源备份实际布局对应。资源不设置 `retentionPolicy`，避免恢复身份触发源备份清理。

```yaml
apiVersion: barmancloud.cnpg.io/v1
kind: ObjectStore
metadata:
  name: restore-source-store
  namespace: dba-restore
spec:
  configuration:
    destinationPath: s3://replace-restore-bucket/replace-unique-pg-a-prefix
    endpointURL: https://restore-object-store.example.invalid
    endpointCA:
      name: restore-s3-ca
      key: ca.crt
    s3Credentials:
      accessKeyId:
        name: restore-s3-reader
        key: ACCESS_KEY_ID
      secretAccessKey:
        name: restore-s3-reader
        key: ACCESS_SECRET_KEY
```

```bash
export RESTORE_OBJECTSTORE_FILE='<受控目录中的 restore-objectstore.yaml>'
r create --dry-run=server --validate=strict -f "$RESTORE_OBJECTSTORE_FILE"
r create -f "$RESTORE_OBJECTSTORE_FILE"
r -n "$RESTORE_NS" get objectstore restore-source-store
```

将以下内容保存为 `restore.yaml`。`serverName` 为备份来源的实际归档服务器名；如果源没有显式覆盖，通常是源集群名，实施时应从备份元数据核实。`targetTime` 必须替换为在恢复窗口内的 RFC 3339 带时区时间；基础备份应早于该时间，且 WAL 中需有目标后记录以使时间点恢复停止。StorageClass、节点标签和资源值应替换为恢复环境已交接的配置。示例仅创建一个恢复实例，不具备高可用。

```yaml
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: pg-a-restore
  namespace: dba-restore
spec:
  instances: 1
  imageName: <MATCHING_CNPG_POSTGRESQL_18_IMAGE_BY_DIGEST>
  postgresql:
    pg_hba:
      - hostnossl all all all reject
      - hostssl app app all scram-sha-256
      - hostssl all all all reject
  bootstrap:
    recovery:
      source: backup-source
      recoveryTarget:
        targetTime: '<TARGET_TIME_RFC3339_WITH_TIMEZONE>'
  externalClusters:
    - name: backup-source
      plugin:
        name: barman-cloud.cloudnative-pg.io
        parameters:
          barmanObjectName: restore-source-store
          serverName: pg-a
  storage:
    storageClass: <VERIFIED_RBD_RETAIN_STORAGECLASS>
    size: 20Gi
  affinity:
    nodeSelector:
      dba.example/eligible: 'true'
  resources:
    requests:
      cpu: '1'
      memory: 2Gi
    limits:
      memory: 4Gi
```

```bash
export RESTORE_FILE='<受控目录中的 restore.yaml>'
r create --dry-run=server --validate=strict -f "$RESTORE_FILE"
r create -f "$RESTORE_FILE"
r -n "$RESTORE_NS" get cluster pg-a-restore -w
r -n "$RESTORE_NS" describe cluster pg-a-restore
r -n "$RESTORE_NS" get pods,pvc,events
```

恢复成功后可通过受控的容器本地 PostgreSQL 身份执行只读验证：

```bash
RESTORE_PRIMARY="$(r -n "$RESTORE_NS" get cluster pg-a-restore -o jsonpath='{.status.currentPrimary}')"
test -n "$RESTORE_PRIMARY"
r -n "$RESTORE_NS" exec "$RESTORE_PRIMARY" -c postgres -- psql -X -U postgres -d app -v ON_ERROR_STOP=1 \
  -c 'SELECT current_database(), pg_is_in_recovery()'
```

验证备份基线行数/哈希，写入目标时间前的标记存在、目标时间后的标记不存在，并检查业务表约束、扩展和应用可读性；需要模拟标记时在隔离演练数据库中先提交目标前标记、记录带时区时间，再提交目标后标记，确认对应 WAL 到达独立副本后恢复。记录开始、结束、可恢复时间点、数据规模和 RPO/RTO。

恢复示例不配置新的 WAL 归档；恢复期间不会向源前缀写入，但恢复目标也没有持续备份保障。若目标需长期保留，在投入业务前为它建立**新的**写身份与独立空前缀，配置插件归档并完成首次备份/恢复验证。[插件恢复示例](https://cloudnative-pg.io/plugin-barman-cloud/docs/usage/)

### 7.3 网络、安全与监控验收

依据锁定版本和实际服务端口生成最小访问清单：应用与实例 TCP 5432；实例复制 TCP 5432；Operator 到实例管理器 TCP 8000；监控到实例 TCP 9187；API server 到 Operator webhook TCP 9443；Operator、实例及插件到 Kubernetes API、DNS、插件服务和 HTTPS 对象存储。插件服务端口、节点到 Ceph MON/OSD、监控方式和 API 出入口以交接配置为准。

逐一验证允许和拒绝，特别确认默认拒绝策略没有阻断 Operator 状态访问、Lease 更新、插件 WAL 上传或证书续期。为每套集群配置写主可用性、同步备节点数、复制延迟、卷和 WAL 水位、归档/备份失败、独立副本可恢复点、证书到期与 Ceph 健康告警；通过真实故障或受控探针验证通知到达值班接收方。

## 8. 故障切换、回退与日常处理

### 8.1 预期自动切换与受控演练

在隔离测试环境先检查：三个实例分布、同步备节点、`failoverQuorum` 与有效配置、旧主隔离机制、备份、连续写入客户端和异常回滚/幂等策略。记录每笔事务唯一 ID、提交确认时间及连接失败；然后在授权窗口按事先批准的单故障方案实施。本文不提供对生产主 Pod 的盲删命令。

逐项观察主实例状态、Lease、Operator 事件、旧主进程及客户端可达性、候选备库接收/回放位置、`rw` / `ro` 端点和客户端重新提交时间。每一笔已经收到成功响应的同步事务都要核对；响应丢失的事务列为“结果未知”并由业务对账。将主进程退出、节点断联、旧主仍可被客户端直连、API 分区、一个同步备节点失联、主和同步备同时失联分别作为独立场景，不能由单次 Pod 删除推断全部通过。

以下是**独立测试集群**的单主 Pod 故障注入示例，仅覆盖 Pod 被删除这一场景。使用专门创建、没有生产数据和流量的测试 Namespace；平台须事先为其设置 `dba.example/environment=test`。启动测试客户端的连续事务记录并确认故障前至少有一笔成功提交，然后在测试端执行：

```bash
export TEST_NS='<专用测试 Namespace>'
export TEST_CLUSTER='<专用测试集群名>'
test "$TEST_NS" != "$NS"
test "$(k get namespace "$TEST_NS" -o json | jq -r '.metadata.labels["dba.example/environment"] // empty')" = 'test'
TEST_UID="$(k -n "$TEST_NS" get cluster "$TEST_CLUSTER" -o jsonpath='{.metadata.uid}')"
TEST_PRIMARY="$(k -n "$TEST_NS" get cluster "$TEST_CLUSTER" -o jsonpath='{.status.currentPrimary}')"
test -n "$TEST_UID" && test -n "$TEST_PRIMARY"
k -n "$TEST_NS" get pod "$TEST_PRIMARY" -o json | jq -e --arg uid "$TEST_UID" \
  'any(.metadata.ownerReferences[]?; .uid == $uid and .kind == "Cluster")'
k -n "$TEST_NS" delete pod "$TEST_PRIMARY" --wait=false
k -n "$TEST_NS" get cluster "$TEST_CLUSTER" -w
```

观察命令需操作者自行退出。新主 Ready 后，从测试客户端重新成功提交并核对每笔故障前后的已确认 ID、连接失败和结果未知的 ID；记录从删除请求到成功提交的时间。确认原实例回归为备节点、复制和备份恢复后，才能进行下一项测试。此结果不能替代 VM 掉电、旧主直连、CSI 或 Ceph 故障测试。

**默认 Operator 可能自动晋升。** 旧主未知或候选数据不完整时不能声称现有配置能等待人工作出决定；必须在生产接入前证明实际隔离/仲裁覆盖该场景。演练无法满足时保持生产阻断，补充可执行隔离方案或重新选型。

### 8.2 真实故障

1. 记录故障时间和业务表现，检查目标 context/UID、集群和 Ceph 身份；暂停相关变更，避免多套集群同时操作。
2. 区分数据库进程、节点、控制面、CSI、Ceph、DNS/网络和对象存储故障。收集 Cluster 事件、Lease、Pod/Service、同步状态、卷状态、日志和备份点；不要输出 Secret 内容。
3. 通过可信节点管理或已验证隔离手段核对旧主是否仍有进程、持有卷或可接收应用流量。Service 无端点、Pod Unknown 和 PVC RWO 都不能单独证明旧主停止写入。
4. 若 CNPG 仲裁不足或候选完整性无法证明，保留无主状态并处理根因；不要通过手动 `promote`、修改同步策略、强删 Pod、强拆卷、删除 WAL 或降低 Ceph 副本要求绕过保护。若业务决定接受数据丢失，必须单独确认目标和风险，再按对应事件预案处理。
5. 自动切换后确认唯一写主、旧主已隔离并正确回归为备节点、`rw` / `ro` 端点、同步提交、对象归档、业务对账与告警恢复。旧主若有分叉数据，先保留证据并评估，不能直接视为可无损回放。

### 8.3 计划切换与维护

计划内切换和自动故障切换是不同场景。先验证最新备份、两个备节点状态、候选位置、旧主可安全停止、客户端重连及容量；再按目标版本 `kubectl cnpg promote` 文档执行受控切换。该命令在仲裁失败时也可能强制晋升，因此只在已确认候选安全且旧主隔离可验证时使用。[CNPG 命令说明](https://cloudnative-pg.io/docs/1.30/kubectl-plugin/)

以下操作在第 2 节核对过的目标及维护窗口执行，`TARGET_INSTANCE` 必须是该集群中经人工核验已追平且 Ready 的备节点；所有权和 Ready 检查不能替代 WAL 完整性判断。操作前先记录当前主、目标和业务写入状态：

```bash
export TARGET_INSTANCE='<已核验的备节点 Pod 名>'
CLUSTER_UID="$(k -n "$NS" get cluster "$CLUSTER" -o jsonpath='{.metadata.uid}')"
CURRENT_PRIMARY="$(k -n "$NS" get cluster "$CLUSTER" -o jsonpath='{.status.currentPrimary}')"
test -n "$CLUSTER_UID" && test -n "$CURRENT_PRIMARY" && test "$TARGET_INSTANCE" != "$CURRENT_PRIMARY"
k -n "$NS" get pod "$TARGET_INSTANCE" -o json | jq -e --arg uid "$CLUSTER_UID" \
  'any(.metadata.ownerReferences[]?; .uid == $uid and .kind == "Cluster") and (.metadata.labels["cnpg.io/instanceRole"] == "replica") and any(.status.conditions[]?; .type == "Ready" and .status == "True")'
kubectl cnpg status "$CLUSTER" --context "$KUBE_CONTEXT" -n "$NS"
```

核对状态输出、WAL 进度和隔离条件后，再执行切换并观察，不能在上面的条件未满足时继续：

```bash
kubectl cnpg promote "$CLUSTER" "$TARGET_INSTANCE" --context "$KUBE_CONTEXT" -n "$NS"
k -n "$NS" get cluster "$CLUSTER" -w
```

待 `currentPrimary` 为目标后，复核第 6.1 节的主备、Service、Lease、同步复制、备份与客户端写入；如出现异常，停在当前状态排查，不自动反向切换或强制删除原主。

节点维护先确认 PDB、同步副本和剩余容量；PDB 只约束自愿驱逐，不保障意外故障。按每套集群逐台维护并等待同步、归档和业务恢复；不要同时维护一个集群的全部候选节点或所有 Ceph OSD。

### 8.4 回退和升级

未投入业务的新建集群若需要撤销，先停止应用接入、确认资源归属、备份和 PVC 保留，再按变更单逐项处理；不因为重试失败删除 Namespace、PVC、Ceph image 或对象前缀。若已承接写入，回退意味着数据迁移或时间点恢复，不能简单删除新主或切回旧主。Operator 和插件为共享组件，禁止为回退一套数据库而卸载。

PostgreSQL 小版本逐套升级，先验证备份、镜像、扩展、主备、同步与客户端重连；若需回退，先查对应版本的官方支持边界，不能假定换回旧镜像可恢复数据。大版本迁移采用经过验证的独立目标与数据迁移方案，保留原数据并在对账后切换；不中途原地降级或覆盖源 PVC。

## 9. 最终验收清单

| 项目 | 验收证据 |
| --- | --- |
| 身份与隔离 | Context/UID、Ceph FSID、独立 Namespace/凭据/对象前缀、越权和网络负向测试 |
| 三实例与存储 | 实际 Pod 节点、物理故障域映射、三个独立 RBD PVC、PV 保留/重挂、Ceph 故障容量和性能 |
| 复制与连接 | 主唯一、`rw` / `ro` 端点、同步 WAL 和真实业务会话配置、TLS 校验、读写及延迟 |
| 备份恢复 | 首次与周期基础备份、连续 WAL、独立副本、目标前/后数据校验、恢复记录和实测 RPO/RTO |
| 主备故障 | 连续已确认事务清单、旧主隔离、候选完整性、端点变化、客户端重连、恢复时间和分区/复合故障边界 |
| 运行交接 | 版本/digest、配置归属、密钥恢复路径、告警送达、维护流程、未通过项目及责任人 |

未做的故障注入、跨故障域恢复或生产验证标记为未通过/未执行，不能以静态 YAML 校验或历史实验结果替代。各环境先完成交接和验收，才允许业务迁入。
