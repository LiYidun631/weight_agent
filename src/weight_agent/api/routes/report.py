"""报告路由：提供身体成分分析报告生成接口。"""

from fastapi import APIRouter, Header, Request

from weight_agent.api.schemas.report import ReportRequest, ReportResponse
from weight_agent.report.workflow import ReportWorkflow

router = APIRouter()


@router.post("/report", response_model=ReportResponse, summary="生成身体成分分析报告")
async def create_report(
    request: Request,
    payload: ReportRequest,
    x_user_id: str | None = Header(default=None),
) -> ReportResponse:
    """接收身体成分数据并返回模型生成（或兜底）的分析报告。"""
    # 未提供用户 ID 时按匿名用户处理
    user_id = x_user_id or "anonymous"
    workflow: ReportWorkflow = request.app.state.report_workflow
    return await workflow.run(payload, user_id=user_id)
