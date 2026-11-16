export const Role={admin:'业务负责人',analyst:'分析员',viewer:'观察员'};
export const JobStatus={queued:'排队中',running:'计算中',completed:'已完成',failed:'执行失败'};
export const DispatchStatus={pending_send:'待发送',awaiting_verification:'待查证',fulfilled:'已落实',cancelling:'撤单中',cancelled:'已撤单',manual:'待人工'};
export const LegStatus={pending:'待发送',sending:'发送中',unknown:'待查证',accepted:'已接单',rejected:'已拒单',cancelling:'撤回中',cancelled:'已撤回',locked:'已开工'};
export const PartyLabel={kitchen:'中央厨房',station:'配送站'};
export const SimulationMode={normal:'正常接单',accept_drop:'接单后丢响应（需重试查证）',reject:'拒单（自动撤回另一端）',locked:'接单并已开工（撤单转人工）'};
