# HospKart BD Team UAT Checklist

Use this checklist to validate the BD assistant before rollout.

## 0) Pre-check (must pass first)

Run:

```powershell
Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:5000/health"
```

Expected:
- `status` = `ok`
- `model` = `hospkart-bd`
- `use_ollama` = `true`

---

## 1) How to run chat tests

Use this template in PowerShell:

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:5000/api/chat" -ContentType "application/json" -Body '{"query":"<PUT QUERY HERE>"}'
```

Record:
- query
- response
- pass/fail

---

## 2) UAT Test Cases (10)

### TC-01 Product Inquiry (basic)
Query:
`IV Set ka full detail do`

Pass if:
- product is relevant to IV set
- response includes vendor/price or listing structure
- no unrelated products in results

### TC-02 Product Inquiry (variant specific)
Query:
`I. V. Set Non Vented Standard ki listing low-to-high pricing me dikhao`

Pass if:
- same/similar product variants are grouped correctly
- pricing order is low to high
- best-offer style detail is shown

### TC-03 Vendor Inquiry
Query:
`MedX Healthcare vendor ke products dikhao`

Pass if:
- vendor-specific products are returned
- output does not drift to other random vendors unless comparison is requested

### TC-04 Mixed Intent (product + feature)
Query:
`I. V. Set with Y port and Luer lock ka best vendor aur per piece rate batao`

Pass if:
- relevant matching products/offers are returned
- best offer is identified from available data

### TC-05 Category Inquiry
Query:
`PPE category ke products aur unke vendors dikhao`

Pass if:
- category matched correctly
- multiple items shown with vendor + price context

### TC-06 Price Comparison
Query:
`Syringe 10ml ka vendor-wise price comparison do`

Pass if:
- vendor-wise comparison appears
- pricing values are consistent and comparable

### TC-07 Stock/Warranty Focus
Query:
`Gloves product me stock aur warranty detail ke saath best available offer batao`

Pass if:
- stock/warranty fields are shown when available
- missing fields handled gracefully (no crash, no nonsense values)

### TC-08 Quotation (normal)
Query:
`I. V. Set Non Vented Standard ke liye 50 units ka quotation banao with 5% discount`

Pass if:
- quotation is generated
- subtotal, discount, GST, final amount visible
- export paths shown (`exports/*.json`, `exports/*.pdf`)

### TC-09 Quotation (high qty)
Query:
`PPE kit ke liye 500 quantity ka quotation chahiye, best available vendor choose karo`

Pass if:
- quote generated without timeout/crash
- quantity correctly reflected
- pricing arithmetic is coherent

### TC-10 Clarification Handling
Query:
`Mujhe best product batao`

Pass if:
- bot asks a short clarification or gives sensible narrowed options
- no fabricated specific product detail without basis

---

## 3) Non-functional checks

Pass if all true:
- response time acceptable for BD workflow
- no API hang (timeout fallback works)
- no server crash during repeated queries
- interactions logged in `logs/interactions.csv`

---

## 4) Final sign-off criteria

Mark "Ready for BD Team" only when:
- at least 8/10 test cases pass
- both quotation tests pass (TC-08, TC-09)
- sampled outputs stay grounded in catalog data
- no blocking errors in logs

---

## 5) Quick regression set (run daily)

Run these 4 every day:
- `IV Set ka full detail do`
- `MedX Healthcare vendor ke products dikhao`
- `Syringe 10ml ka vendor-wise price comparison do`
- `I. V. Set Non Vented Standard ke liye 50 units ka quotation banao with 5% discount`

If any fails, investigate before using for production BD workflow.
