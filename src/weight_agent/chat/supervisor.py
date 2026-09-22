"""Chat Supervisor：将意图识别结果转换为受控执行计划。"""

from weight_agent.chat.models import (
    ChatIntent,
    IntentResult,
    RiskLevel,
    SupervisorPlan,
)


class ChatSupervisor:
    """确定性 Chat 主控规划器。

    Supervisor 不生成最终答复、不查询数据库、不调用工具。它只根据已经校验过的
    IntentResult 生成可审计的 SupervisorPlan，让后续执行层按固定路由运行。
    """

    def build_plan(self, intent_result: IntentResult) -> SupervisorPlan:
        """根据意图识别结果生成执行计划。"""
        if intent_result.risk_level is RiskLevel.URGENT:
            return self._safety_plan(
                intent_result,
                "你描述的情况可能比较紧急，请优先联系当地急救服务或尽快就医。"
                "我可以提供一般健康管理信息，但不能替代医生诊断或急救处理。",
            )

        if intent_result.risk_level is RiskLevel.MEDICAL_REVIEW:
            return self._safety_plan(
                intent_result,
                "这个问题可能涉及疾病、诊断或用药调整，建议咨询医生或专业医疗人员。"
                "我可以继续提供体重管理和生活方式层面的一般建议。",
            )

        if intent_result.needs_clarification:
            return SupervisorPlan(
                route="clarification",
                intent_result=intent_result,
                required_agents=[],
                requires_metric_query=False,
                clarification_question=intent_result.clarification_question,
            )

        if intent_result.intent is ChatIntent.GREETING:
            return SupervisorPlan(
                route="greeting",
                intent_result=intent_result,
                required_agents=[],
                requires_metric_query=False,
            )

        if intent_result.intent is ChatIntent.OUT_OF_SCOPE:
            return SupervisorPlan(
                route="out_of_scope",
                intent_result=intent_result,
                required_agents=[],
                requires_metric_query=False,
            )

        if intent_result.intent in {ChatIntent.METRIC_QUERY, ChatIntent.METRIC_ANALYSIS}:
            return SupervisorPlan(
                route="data_analysis",
                intent_result=intent_result,
                required_agents=["data_analysis"],
                requires_metric_query=True,
            )

        if intent_result.intent is ChatIntent.DATA_BASED_ADVICE:
            return SupervisorPlan(
                route="data_based_advice",
                intent_result=intent_result,
                required_agents=["data_analysis", "business_advice"],
                requires_metric_query=True,
            )

        return SupervisorPlan(
            route="domain_advice",
            intent_result=intent_result,
            required_agents=["business_advice"],
            requires_metric_query=False,
        )

    @staticmethod
    def _safety_plan(intent_result: IntentResult, safety_message: str) -> SupervisorPlan:
        return SupervisorPlan(
            route="safety",
            intent_result=intent_result,
            required_agents=[],
            requires_metric_query=False,
            safety_message=safety_message,
        )
