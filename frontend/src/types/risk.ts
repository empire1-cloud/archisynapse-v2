/**
 * Fraud/risk types — services/fraud (archisynapse_fraud_mvp.py) via
 * royalty_decision.py's REAL POST /risk/royalty call, and the older
 * canonical_event.py fraud_* trace fields used by the non-royalty
 * "Revenue Assurance Loop" (gateway main.py / orchestrator.py).
 */

export type FraudDecision = 'allow_payout' | 'hold_payout' | 'block_payout';

export interface RiskAssessment {
  transaction_id: string;
  tenant_id: string;
  fraud_decision: FraudDecision;
  risk_score: number; // 0-100 from the fraud service; royalty_decision.py divides by 100
  reasons: string[];
  policy: string;
  evaluated_at: string;
}
