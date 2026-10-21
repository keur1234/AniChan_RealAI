Vicky (โปรแกรมแชทในเครื่อง) — ไม่ต้องลง Python
=====================================================

เริ่มใช้งาน
1. แตกไฟล์ zip ไว้ที่ไหนก็ได้ เช่น D:\Vicky
2. ใส่ไฟล์เหล่านี้ไว้ในโฟลเดอร์เดียวกับ Vicky.exe
   - .env                 (ก๊อปจาก .env.example แล้วใส่ GEMINI_API_KEY และค่าอื่นๆ)
   - obk_ref_bundle.json  (ตารางอ้างอิงสำหรับตรวจ index codes)
   - documents.xlsx       (ถ้ามี: รายชื่อเอกสารของทีม ดูตัวอย่างจาก documents_template.xlsx)
   - bim_index.db         (ถ้ามี: ข้อมูล BIM หรือพิมพ์ /bimsync ในแชทเพื่อดึงใหม่ ถ้าตั้ง BIM_DRIVE_FOLDER ไว้)
3. ดับเบิลคลิก Vicky.exe
   - หน้าต่างดำ (console) คือตัวโปรแกรม ปิดหน้าต่างนี้ = ปิด Vicky
   - หน้าต่างแชทจะเปิดขึ้นมาเอง (ใช้ Microsoft Edge แบบหน้าต่างแอป)
   - ดับเบิลคลิกซ้ำตอนเปิดอยู่แล้ว จะแค่เปิดหน้าต่างแชทขึ้นมาใหม่

ใช้งาน
- ลากไฟล์ Excel/CSV มาวางในหน้าแชท แล้วกด /check หรือ /check FAS
- ผลตรวจเก็บไว้ในโฟลเดอร์ results (ถ้าใส่ OBK_RESULTS_DIR=results ใน .env) กดการ์ดไฟล์เพื่อเปิดโฟลเดอร์
- คำสั่ง: /check, /check FAS, /summary, /bim, /docs, /refs, /refresh, /bimsync, /reset

หมายเหตุ
- Windows SmartScreen อาจเตือนตอนเปิดครั้งแรก เพราะโปรแกรมไม่ได้ลงลายเซ็น กด "More info" > "Run anyway"
- อย่าส่งต่อโฟลเดอร์นี้พร้อมไฟล์ .env เพราะมี API key อยู่ในนั้น
