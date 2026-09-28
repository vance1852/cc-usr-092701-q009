# 服务接口

所有时间使用带时区的 ISO 8601 格式。服务持久化 UTC 时间，按诊所配置的时区解释运营日期。JSON 请求大小上限为 1 MB；无效请求返回稳定的错误码和 HTTP 状态，不向调用方透出数据库异常。

## 登录与诊所隔离

`POST /auth/token` 接受 `staff_id` 和 `password`，返回有效期不超过一天的 Bearer 凭据。除登录与健康检查外，请求必须同时提供 `Authorization: Bearer …` 和 `X-Clinic-ID`。认证失败不区分账号不存在、停用或密码错误；诊所边界之外的数据返回不存在，避免泄露另一诊所的记录。

`POST /auth/logout` 撤销当前凭据。修改员工密码会撤销该员工的全部活动凭据。初始负责人通过命令行创建；没有可直接注册负责人的 HTTP 路由。

## 患者、评估与诊疗计划

- `POST /patients` 建立诊所内患者档案；外部编号在诊所范围内唯一。
- `GET /patients/{patient_id}` 返回最小档案，不返回联系方式密文。
- `POST /patients/{patient_id}/merge` 以两个版本号和书面原因将重复档案标记为合并，并指向保留档案。
- `POST /patients/{patient_id}/assessments` 新建评估草稿；`POST /assessments/{assessment_id}/sign` 由临床岗位签署。
- `POST /patients/{patient_id}/consents` 创建更高版本的授权；`POST /consents/{consent_id}/withdraw` 撤回授权。
- `POST /patients/{patient_id}/plans` 建立计划，医美和体重管理计划必须引用当前对应授权。
- `POST /plans/{plan_id}/{propose|activate|pause|resume|complete|cancel}` 以 `expected_version` 执行带版本保护的状态转换。
- `GET /patients/{patient_id}/weight-series` 返回按观察时间排序的测量值，不生成诊断或治疗建议。

评估签署后不可覆盖。就诊病历由章节组成，签署需要主诉、评估和计划三部分；签署后的补充内容成为新版本，原始文字仍保留。

## 预约、随访与计划节点

创建预约须提供 `Idempotency-Key`，有责任人的预约不能与未结束时段重叠。临时占位到期后由 `POST /appointments/{id}/book` 拒绝确认，过期占位可通过服务方法按限额释放。预约状态按占位、确认、到诊、服务、完成推进；开始服务时产生就诊记录。

随访和计划节点支持领取租约、版本校验、幂等创建、延期和完整处置历史。旧领取者不能以过期令牌提交结果；重新领取不会删除前次领取事件。

## 诊所耗材

- `POST /products` 登记耗材；`POST /products/{product_id}/lots` 按批号入库。
- `POST /stock/reserve` 依据失效日期按先到期先出分批预留，需要 `Idempotency-Key`。
- `POST /stock/{reservation_id}/consume` 记录患者使用；`release` 释放尚未使用的数量。
- `POST /stock/{lot_id}/quarantine`、`recall` 或 `release-quarantine` 记录批次处置及受影响预留。
- `GET /stock/lots` 查看可用数量；`GET /stock/{lot_id}/history` 查看批次流水。

入库、占用、释放与患者使用均进入不可变流水。存在不足时整笔预留回滚；被隔离、召回或在诊所本地日期已过期的批次不能继续使用。

## 患者次数权益台账

套餐购买、赠送、补偿、老店转入与历史补录都登记为**发放批次（grant）**，记录来源（`purchase`/`gift`/`compensation`/`transfer_in`/`backfill`）、来源单号、适用项目编号范围、生效日与到期时间、规则版本。补偿与转入只能由诊所负责人登记；补录不能直接发放，必须走申请-审批。

- `POST /services` 维护服务项目目录（编号、名称、分类、规则版本）；`GET /services` 列出在用项目。
- `POST /patients/{patient_id}/entitlements/grants`（需 `Idempotency-Key`）发放购买或赠送额度。
- `GET /patients/{patient_id}/entitlements` 返回按项目汇总的 `issued / held / in_review / used / available`，并逐批次列出。
- `GET /patients/{patient_id}/entitlements/ledger?service_code=…` 返回该患者全部不可变台账流水；`GET /entitlements/grants/{grant_id}` 查看单批次的每笔去向。
- `POST /entitlements/grants/expire` 结转已到期批次；`POST /entitlements/grants/{grant_id}/revoke`（负责人）停用批次。

预约在创建时携带 `service_code` 与 `service_quantity`：仅登记意图，不扣权益。`POST /appointments/{id}/book` 确认时在同一事务内按**先到期先出（FEFO）**跨批次占用；未生效、已停用或已到期批次不参与，可用次数不足时整笔确认回滚。取消（含占位超时自动取消）将未决占用全部**释放**回可用余额；未到诊（no_show）不直接扣次，占用转**待财务复核**。

- `GET /appointments/{id}/holds` 查看预约在各发放批次上的占用与处置状态。
- `POST /appointments/{id}/settle` 在服务签署后按实际完成内容结算，每行须显式分列 `redeemed`（核销）、`released`（释放返还）、`review`（送财务复核）；存在核销数量时必须已有**已签署就诊记录**，且只能由临床岗位提交。同一预约可多次结算，累计不得超过预留数量。
- `POST /entitlements/reviews` 由财务岗位对单笔待复核流水作 `release` 或 `deduct` 终局决定，每笔只能决定一次。
- `POST /entitlements/backfills`、`POST /entitlements/reversals`（需 `Idempotency-Key`、理由至少 10 字、可附凭据编号）由前台/协调员发起；`GET /entitlements/adjustments` 列出单据，`POST /entitlements/adjustments/{id}/review` 由**另一人**（财务/审计/负责人）批准或驳回（驳回必须填意见）。申请人不能审批自己的单据；批准后仍以追加的 `issue`/`return`/`reverse` 流水入账，冲正数量不得超过原流水尚未冲正的部分。

台账流水（`issue/hold/release/redeem/deduct/review/return/reverse/expire`）只允许追加，数据库触发器从存储层拒绝任何 UPDATE 或 DELETE；余额始终由流水求和得到，已结账记录没有直接改数的入口。每笔流水都带操作人、原因、预约/就诊/审批关联，前台与财务查看同一余额时可追到每次发放、预留、核销与返还。所有权益动作同时进入诊所哈希链审计，`GET /audit/diagnostics` 会报告负余额、过期批次仍有余额、长期滞留的待复核与待审批单据。

## 不良事件与数据使用

护理人员可报告事件或患者安全关注项；临床岗位复核并记录处置，诊所负责人可作废就诊记录。`GET /audit/verify` 校验诊所哈希链，`GET /audit/diagnostics` 汇报需人工核对的一致性问题，不自动修改业务状态。

`POST /patients/{patient_id}/export` 只在存在有效数据导出授权时返回明确选择的章节。导出字段采用白名单，联系方式密文、凭据和内部合并字段不会导出；相同幂等请求得到相同内容摘要。`GET /reports/daily`、`appointments`、`incidents` 和 `overdue-milestones` 仅返回运营汇总或经岗位授权的工作队列。

## 主要状态

- 计划：草稿 → 提议 → 生效；可暂停和恢复，完成或取消后不能重新激活。
- 预约：占位 → 确认 → 到诊 → 服务中 → 完成；取消和未到诊是独立终态。
- 不良事件：已报告 → 分诊 → 观察 → 已解决 → 关闭。每次处置单独记录操作人和理由。
- 耗材预留：预留 → 释放或核销。库存数量由收货、预留、释放和更正流水求和，不直接改写历史数量。
- 次数权益：发放批次经预约确认占用（FEFO），服务签署后核销；取消释放、未到诊与部分履约送财务复核决定；补录与冲走申请/审批分离，台账只追加、不可改删。
