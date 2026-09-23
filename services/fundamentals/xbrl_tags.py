"""Candidate us-gaap XBRL tag names per `FinancialStatement` field.

Real SEC filers use different tags for the same accounting concept across
companies and reporting years — the taxonomy evolves, and filers choose
their own tag when several apply. This list is deliberately NOT
exhaustive: `edgar_source.py` tries each candidate in order and treats a
concept as genuinely missing for a given quarter only once none of its
candidates have a value there — never a guess at data that isn't present.
"""
from __future__ import annotations

# Duration (income-statement / cash-flow) concepts.
REVENUE_TAGS = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "SalesRevenueNet")
COST_OF_REVENUE_TAGS = ("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold")
OPERATING_INCOME_TAGS = ("OperatingIncomeLoss",)
NET_INCOME_TAGS = ("NetIncomeLoss", "ProfitLoss")
EPS_DILUTED_TAGS = ("EarningsPerShareDiluted",)
SHARES_DILUTED_TAGS = ("WeightedAverageNumberOfDilutedSharesOutstanding",)
OPERATING_CASH_FLOW_TAGS = ("NetCashProvidedByUsedInOperatingActivities",
                            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations")
CAPEX_TAGS = ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsForCapitalImprovements")
INTEREST_EXPENSE_TAGS = ("InterestExpense", "InterestExpenseDebt")
DEPRECIATION_AMORTIZATION_TAGS = ("DepreciationDepletionAndAmortization",
                                  "DepreciationAmortizationAndAccretionNet",
                                  "DepreciationAndAmortization")
INCOME_TAX_EXPENSE_TAGS = ("IncomeTaxExpenseBenefit",)
PRETAX_INCOME_TAGS = (
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
)

# Instant (balance-sheet) concepts.
TOTAL_ASSETS_TAGS = ("Assets",)
CASH_TAGS = ("CashAndCashEquivalentsAtCarryingValue",
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents")
LONG_TERM_DEBT_TAGS = ("LongTermDebtNoncurrent", "LongTermDebt")
CURRENT_DEBT_TAGS = ("LongTermDebtCurrent", "ShortTermBorrowings", "DebtCurrent")
