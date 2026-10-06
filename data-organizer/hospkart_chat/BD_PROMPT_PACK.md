# HospKart BD Prompt Pack

Use these prompts in chat to get consistent BD helper outputs.

## 1) Product Inquiry (Vendor Comparison)

`IV set product ki listing chahiye. HospKart dataset ke hisab se by-product vendor table do, low-to-high pricing me.`

`3 ml syringe ki complete vendor listing do with per-piece price, remarks/specs, location, stock, warranty.`

## 2) Deep Detail Inquiry

`Mujhe "I. V. Set with Y port & Luer lock" ka full detail chahiye: description, specs, price, vendor status, product status, stock aur best offer.`

`Phototherapy product ka deep research report do from HospKart data. Similar products bhi include karo with price comparison.`

## 3) Category-Level Research

`Injection & IV category me top 10 products ka vendor-wise comparison do, sorted by lowest price.`

`Surgical caps category ka by-product breakdown do. Har product ke niche vendor table do.`

## 4) Quote / RFQ Prompts

`I. V. Set Non Vented Standard ke liye 50 units ka quotation banao with 5% discount and GST.`

`Fowler bed ke liye 10 units ka best-price quotation banao aur JSON/PDF export references bhi do.`

`RFQ: 2 products chahiye - IV Set (100 qty), Syringe 3 ml (200 qty). Best vendor combination suggest karo and final amount do.`

## 5) Clarification / Follow-up Prompts

`Is quote me second best vendor option bhi add karo.`

`Jo vendor active hai sirf unhi ka comparison fir se do.`

`Stock 0 wale vendors hata ke fresh listing do.`

## Expected Output Shape (Target)

The assistant should ideally respond in this structure:

1. `By product - vendors and pricing (sorted low to high)`
2. For each product block:
   - `<Category> > <Product Name> - <N> vendor(s)`
   - `S.No | Vendor Name | Per Piece Price | Remarks | Location`
   - `Best Offer Detail: Vendor=..., Stock=..., Warranty=..., Status=...`

## Best Practices for BD Team

- Always include quantity when asking for quotation.
- Mention product exactly (or with key terms) for better semantic retrieval.
- Ask for `active vendors only` when you need procurement-ready options.
- Ask follow-up: `second best option`, `delivery location wise`, `stock-filtered`.
