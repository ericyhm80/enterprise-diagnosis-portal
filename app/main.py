
import os, time, uuid, json, hmac, mimetypes
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Header
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import create_engine, text

BASE = Path(__file__).resolve().parent.parent
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{BASE / 'data' / 'diagnosis.db'}")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

ADMIN_TOKEN = os.environ.get("DIAG_ADMIN_TOKEN", "change-me-before-production")
MAX_UPLOAD_MB = int(os.environ.get("DIAG_MAX_UPLOAD_MB", "12"))

engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)

app = FastAPI(title="Enterprise Diagnosis Portal v1.4 Free Pilot")
app.mount("/static", StaticFiles(directory=BASE/"app"/"static"), name="static")

def init_db():
    ddl = [
    """CREATE TABLE IF NOT EXISTS respondents(
        id TEXT PRIMARY KEY,
        name TEXT,
        role TEXT,
        company TEXT,
        created_at BIGINT,
        updated_at BIGINT
    )""",
    """CREATE TABLE IF NOT EXISTS answers(
        respondent_id TEXT,
        question_id TEXT,
        text TEXT,
        updated_at BIGINT,
        PRIMARY KEY(respondent_id, question_id)
    )""",
    """CREATE TABLE IF NOT EXISTS files(
        id TEXT PRIMARY KEY,
        respondent_id TEXT,
        question_id TEXT,
        kind TEXT,
        original_name TEXT,
        content_type TEXT,
        bytes BIGINT,
        content BYTEA,
        created_at BIGINT
    )""",
    """CREATE TABLE IF NOT EXISTS materials(
        respondent_id TEXT,
        material_id TEXT,
        checked INTEGER,
        updated_at BIGINT,
        PRIMARY KEY(respondent_id, material_id)
    )""",
    """CREATE TABLE IF NOT EXISTS transcripts(
        file_id TEXT PRIMARY KEY,
        provider TEXT,
        status TEXT,
        transcript TEXT,
        created_at BIGINT,
        updated_at BIGINT
    )""",
    """CREATE TABLE IF NOT EXISTS audit_log(
        id BIGSERIAL PRIMARY KEY,
        actor TEXT,
        action TEXT,
        target TEXT,
        details TEXT,
        created_at BIGINT
    )"""
    ]
    # SQLite does not support BIGSERIAL/BYTEA. Keep a simple compatibility path.
    if DATABASE_URL.startswith("sqlite"):
        ddl = [x.replace("BYTEA","BLOB").replace("BIGSERIAL PRIMARY KEY","INTEGER PRIMARY KEY AUTOINCREMENT") for x in ddl]
    with engine.begin() as con:
        for stmt in ddl:
            con.execute(text(stmt))

init_db()

@app.get("/")
def home():
    return FileResponse(BASE/"app"/"static"/"index.html")

@app.get("/admin")
def admin_page():
    return FileResponse(BASE/"app"/"static"/"admin.html")

class RespondentIn(BaseModel):
    name: str = ""
    role: str = ""
    company: str = ""

@app.post("/api/respondents")
def create_respondent(payload: RespondentIn):
    rid = uuid.uuid4().hex[:16]
    now = int(time.time())
    with engine.begin() as con:
        con.execute(text("""
            INSERT INTO respondents(id,name,role,company,created_at,updated_at)
            VALUES(:id,:name,:role,:company,:created_at,:updated_at)
        """), dict(id=rid,name=payload.name,role=payload.role,company=payload.company,created_at=now,updated_at=now))
        con.execute(text("""
            INSERT INTO audit_log(actor,action,target,details,created_at)
            VALUES(:actor,:action,:target,:details,:created_at)
        """), dict(actor=rid,action="create_respondent",target=rid,details="{}",created_at=now))
    return {"respondent_id": rid}

class AnswerIn(BaseModel):
    respondent_id: str
    question_id: str
    text: str = ""

@app.post("/api/answers")
def save_answer(payload: AnswerIn):
    now = int(time.time())
    with engine.begin() as con:
        exists = con.execute(text("SELECT id FROM respondents WHERE id=:id"), {"id":payload.respondent_id}).fetchone()
        if not exists:
            raise HTTPException(404, "respondent not found")
        con.execute(text("""
            INSERT INTO answers(respondent_id,question_id,text,updated_at)
            VALUES(:rid,:qid,:txt,:now)
            ON CONFLICT(respondent_id,question_id)
            DO UPDATE SET text=excluded.text, updated_at=excluded.updated_at
        """), dict(rid=payload.respondent_id,qid=payload.question_id,txt=payload.text,now=now))
        con.execute(text("UPDATE respondents SET updated_at=:now WHERE id=:id"), {"now":now,"id":payload.respondent_id})
    return {"ok": True, "updated_at": now}

class MaterialIn(BaseModel):
    respondent_id: str
    material_id: str
    checked: bool

@app.post("/api/materials")
def save_material(payload: MaterialIn):
    now = int(time.time())
    with engine.begin() as con:
        con.execute(text("""
            INSERT INTO materials(respondent_id,material_id,checked,updated_at)
            VALUES(:rid,:mid,:checked,:now)
            ON CONFLICT(respondent_id,material_id)
            DO UPDATE SET checked=excluded.checked, updated_at=excluded.updated_at
        """), dict(rid=payload.respondent_id,mid=payload.material_id,checked=1 if payload.checked else 0,now=now))
    return {"ok": True}

@app.post("/api/upload")
async def upload_file(
    respondent_id: str = Form(...),
    question_id: str = Form(...),
    kind: str = Form("attachment"),
    file: UploadFile = File(...)
):
    content = await file.read()
    if len(content) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"file exceeds {MAX_UPLOAD_MB}MB")
    fid = uuid.uuid4().hex
    now = int(time.time())
    ctype = file.content_type or mimetypes.guess_type(file.filename or "")[0] or "application/octet-stream"
    with engine.begin() as con:
        con.execute(text("""
            INSERT INTO files(id,respondent_id,question_id,kind,original_name,content_type,bytes,content,created_at)
            VALUES(:id,:rid,:qid,:kind,:name,:ctype,:bytes,:content,:now)
        """), dict(id=fid,rid=respondent_id,qid=question_id,kind=kind,name=file.filename or "upload",
                   ctype=ctype,bytes=len(content),content=content,now=now))
        if kind == "audio":
            con.execute(text("""
                INSERT INTO transcripts(file_id,provider,status,transcript,created_at,updated_at)
                VALUES(:fid,'none','pending','',:now,:now)
                ON CONFLICT(file_id) DO UPDATE SET status='pending', updated_at=:now
            """), {"fid":fid,"now":now})
        con.execute(text("""
            INSERT INTO audit_log(actor,action,target,details,created_at)
            VALUES(:actor,'upload_file',:target,:details,:now)
        """), {"actor":respondent_id,"target":fid,"details":json.dumps({"question_id":question_id,"kind":kind},ensure_ascii=False),"now":now})
    return {"ok": True, "file_id": fid}

def require_admin(x_admin_token: Optional[str]):
    if not x_admin_token or not hmac.compare_digest(x_admin_token, ADMIN_TOKEN):
        raise HTTPException(401, "invalid admin token")

@app.get("/api/admin/respondents")
def admin_respondents(x_admin_token: Optional[str] = Header(default=None)):
    require_admin(x_admin_token)
    with engine.connect() as con:
        rows = con.execute(text("""
          SELECT r.*,
           (SELECT COUNT(*) FROM answers a WHERE a.respondent_id=r.id AND length(trim(a.text))>0) answer_count,
           (SELECT COUNT(*) FROM files f WHERE f.respondent_id=r.id) file_count
          FROM respondents r ORDER BY updated_at DESC
        """)).mappings().all()
    return [dict(x) for x in rows]

@app.get("/api/admin/respondent/{rid}")
def admin_respondent(rid: str, x_admin_token: Optional[str] = Header(default=None)):
    require_admin(x_admin_token)
    with engine.connect() as con:
        r = con.execute(text("SELECT * FROM respondents WHERE id=:rid"), {"rid":rid}).mappings().first()
        if not r:
            raise HTTPException(404, "not found")
        answers = con.execute(text("SELECT * FROM answers WHERE respondent_id=:rid ORDER BY question_id"), {"rid":rid}).mappings().all()
        files = con.execute(text("""
          SELECT f.id,f.respondent_id,f.question_id,f.kind,f.original_name,f.content_type,f.bytes,f.created_at,
                 t.status transcript_status,t.transcript,t.provider
          FROM files f LEFT JOIN transcripts t ON t.file_id=f.id
          WHERE f.respondent_id=:rid ORDER BY f.created_at
        """), {"rid":rid}).mappings().all()
        mats = con.execute(text("SELECT * FROM materials WHERE respondent_id=:rid"), {"rid":rid}).mappings().all()
    return {"respondent":dict(r),"answers":[dict(x) for x in answers],"files":[dict(x) for x in files],"materials":[dict(x) for x in mats]}

@app.get("/api/admin/file/{fid}")
def admin_file(fid: str, x_admin_token: Optional[str] = Header(default=None)):
    require_admin(x_admin_token)
    with engine.connect() as con:
        row = con.execute(text("""
            SELECT original_name,content_type,content FROM files WHERE id=:fid
        """), {"fid":fid}).mappings().first()
    if not row:
        raise HTTPException(404,"not found")
    headers={"Content-Disposition": f'attachment; filename="{row["original_name"]}"'}
    return Response(content=row["content"], media_type=row["content_type"], headers=headers)

@app.post("/api/admin/transcribe/{fid}")
def admin_transcribe(fid: str, x_admin_token: Optional[str] = Header(default=None)):
    require_admin(x_admin_token)
    with engine.connect() as con:
        row = con.execute(text("SELECT id FROM files WHERE id=:fid AND kind='audio'"), {"fid":fid}).first()
    if not row:
        raise HTTPException(404,"audio not found")
    return {
        "status":"pending",
        "provider":"none",
        "transcript":"",
        "error":"Free Pilot keeps original audio only. No external AI transcription is configured."
    }

@app.get("/api/admin/export/{rid}")
def admin_export(rid: str, x_admin_token: Optional[str] = Header(default=None)):
    require_admin(x_admin_token)
    return JSONResponse(admin_respondent(rid, x_admin_token))

@app.get("/api/schema")
def schema():
    return {"roles": {"owner": "老板 / 董事长 / 总经理", "business_head": "业务负责人 / 部门负责人", "broker": "经纪人 / 客户经理 / 销售", "placement": "产品 / Placement / 核保支持", "claims": "理赔人员", "operations": "运营 / 出单 / 客服", "finance": "财务 / 佣金结算", "compliance": "合规 / 法务 / 内控", "it": "IT / 系统管理员 / 泛维管理员"}, "questions": [{"section": "0", "domain": "共同问题", "id": "C0_1", "text": "请简单描述你的岗位职责，以及你在一笔保险业务中通常负责哪些工作？", "roles": ["owner", "business_head", "broker", "placement", "claims", "operations", "finance", "compliance", "it"]}, {"section": "0", "domain": "共同问题", "id": "C0_2", "text": "在你日常工作中，最耗时间、最重复、最容易返工的三件事是什么？", "roles": ["owner", "business_head", "broker", "placement", "claims", "operations", "finance", "compliance", "it"]}, {"section": "0", "domain": "共同问题", "id": "C0_3", "text": "哪些事情最依赖个人经验、老员工或特定同事？如果他们不在，工作会受到什么影响？", "roles": ["owner", "business_head", "broker", "placement", "claims", "operations", "finance", "compliance", "it"]}, {"section": "0", "domain": "共同问题", "id": "C0_4", "text": "你日常最常使用哪些系统或工具（企微、泛微、Excel、邮件、保险公司系统等）？最不方便的是什么？", "roles": ["owner", "business_head", "broker", "placement", "claims", "operations", "finance", "compliance", "it"]}, {"section": "0", "domain": "共同问题", "id": "C0_5", "text": "如果公司只能优先解决一个问题，你最希望先解决什么？为什么？", "roles": ["owner", "business_head", "broker", "placement", "claims", "operations", "finance", "compliance", "it"]}, {"section": "A", "domain": "老板 / 高管", "id": "A1", "text": "公司目前总人数、经纪业务人员、活跃经纪人、主要业务条线分别是什么？", "roles": ["owner"]}, {"section": "A", "domain": "老板 / 高管", "id": "A2", "text": "请概述最近三年的收入、利润、保费规模和人员变化趋势。", "roles": ["owner"]}, {"section": "A", "domain": "老板 / 高管", "id": "A3", "text": "公司最赚钱的前三类业务是什么？未来两年最想重点发展的业务是什么？", "roles": ["owner"]}, {"section": "A", "domain": "老板 / 高管", "id": "A4", "text": "目前限制公司增长的前三个瓶颈是什么？", "roles": ["owner"]}, {"section": "A", "domain": "老板 / 高管", "id": "A5", "text": "如果两年 AI 转型成功，最希望看到哪三项经营指标明显改善？", "roles": ["owner"]}, {"section": "A", "domain": "老板 / 高管", "id": "A6", "text": "公司吸引优秀经纪人的核心优势是什么？竞争对手最常用什么方式挖人？", "roles": ["owner"]}, {"section": "A", "domain": "老板 / 高管", "id": "A7", "text": "你希望公司未来的核心数字资产掌握在自己手中到什么程度？哪些数据绝不能外流？", "roles": ["owner"]}, {"section": "A", "domain": "老板 / 高管", "id": "A8", "text": "目前泛微、企微及其他系统每年大约投入多少费用？你对这些系统的满意度如何？", "roles": ["owner"]}, {"section": "B", "domain": "业务负责人", "id": "B1", "text": "你负责的业务条线目前有多少客户、经纪人、年保费和收入规模？", "roles": ["business_head"]}, {"section": "B", "domain": "业务负责人", "id": "B2", "text": "从获客到成交，最常见的卡点在哪里？", "roles": ["business_head"]}, {"section": "B", "domain": "业务负责人", "id": "B3", "text": "经纪人最需要公司提供哪些支持，才能更容易成交和续保？", "roles": ["business_head"]}, {"section": "B", "domain": "业务负责人", "id": "B4", "text": "哪些业务流程最依赖跨部门协作？在哪里最容易等待或返工？", "roles": ["business_head"]}, {"section": "B", "domain": "业务负责人", "id": "B5", "text": "你如何评价当前产品、Placement、运营和理赔对前线业务的支持效率？", "roles": ["business_head"]}, {"section": "B", "domain": "业务负责人", "id": "B6", "text": "你能否实时看到业务漏斗、成交率、续保率和团队人均产出？目前缺哪些数据？", "roles": ["business_head"]}, {"section": "C", "domain": "经纪人 / 客户经理", "id": "C1", "text": "你的客户主要从哪里来？公司提供的客户线索占比大约多少？", "roles": ["broker"]}, {"section": "C", "domain": "经纪人 / 客户经理", "id": "C2", "text": "一天工作中，你大约有多少时间真正花在客户沟通、销售和专业服务上？", "roles": ["broker"]}, {"section": "C", "domain": "经纪人 / 客户经理", "id": "C3", "text": "准备一个客户方案时，你通常需要找哪些资料、找哪些人、花多长时间？", "roles": ["broker"]}, {"section": "C", "domain": "经纪人 / 客户经理", "id": "C4", "text": "询价、报价整理、核保补件、出单、续保、理赔中，哪几个环节最影响你服务客户？", "roles": ["broker"]}, {"section": "C", "domain": "经纪人 / 客户经理", "id": "C5", "text": "哪些信息需要在企微、Excel、泛微或保司系统之间重复录入？", "roles": ["broker"]}, {"section": "C", "domain": "经纪人 / 客户经理", "id": "C6", "text": "客户最常向你问哪些问题？哪些问题你经常重复回答？", "roles": ["broker"]}, {"section": "C", "domain": "经纪人 / 客户经理", "id": "C7", "text": "如果系统能帮你自动完成三类工作，你最希望是哪三类？", "roles": ["broker"]}, {"section": "C", "domain": "经纪人 / 客户经理", "id": "C8", "text": "你是否能方便地看到自己的客户、续保提醒、报价进度、理赔进度和佣金？哪里最不透明？", "roles": ["broker"]}, {"section": "C", "domain": "经纪人 / 客户经理", "id": "C9", "text": "什么样的平台支持会让你更愿意长期留在公司、并愿意把更多客户放在公司体系内经营？", "roles": ["broker"]}, {"section": "D", "domain": "产品 / Placement / 核保支持", "id": "D1", "text": "收到一个新业务后，你需要哪些资料才能开始判断和询价？最常缺什么？", "roles": ["placement"]}, {"section": "D", "domain": "产品 / Placement / 核保支持", "id": "D2", "text": "保险公司承保偏好、产品知识和历史报价目前记录在哪里？是否容易检索？", "roles": ["placement"]}, {"section": "D", "domain": "产品 / Placement / 核保支持", "id": "D3", "text": "一个项目平均询价几家保险公司？有效报价几家？平均需要多久？", "roles": ["placement"]}, {"section": "D", "domain": "产品 / Placement / 核保支持", "id": "D4", "text": "核保问题通常要来回补充几轮？最常见的补件原因是什么？", "roles": ["placement"]}, {"section": "D", "domain": "产品 / Placement / 核保支持", "id": "D5", "text": "报价回来以后如何标准化、比较责任、除外、限额、免赔额和保费？目前有多少人工复制？", "roles": ["placement"]}, {"section": "D", "domain": "产品 / Placement / 核保支持", "id": "D6", "text": "拒保、加费、特殊条件和核保意见是否沉淀为可搜索经验？", "roles": ["placement"]}, {"section": "D", "domain": "产品 / Placement / 核保支持", "id": "D7", "text": "哪些判断适合系统辅助，哪些判断你认为必须由资深人员负责？", "roles": ["placement"]}, {"section": "E", "domain": "理赔人员", "id": "E1", "text": "客户报案后，从受理到结案的完整流程是什么？", "roles": ["claims"]}, {"section": "E", "domain": "理赔人员", "id": "E2", "text": "理赔资料最常缺哪些？补件通常来回几次？", "roles": ["claims"]}, {"section": "E", "domain": "理赔人员", "id": "E3", "text": "案件进度如何记录？客户和经纪人如何知道当前状态？", "roles": ["claims"]}, {"section": "E", "domain": "理赔人员", "id": "E4", "text": "最耗时间的工作是材料整理、沟通、定责判断、催办还是其他？", "roles": ["claims"]}, {"section": "E", "domain": "理赔人员", "id": "E5", "text": "拒赔或争议案件的原因是否被系统化沉淀，并反馈给投保和产品设计？", "roles": ["claims"]}, {"section": "E", "domain": "理赔人员", "id": "E6", "text": "哪些理赔工作可以标准化，哪些必须依赖专家判断或人工沟通？", "roles": ["claims"]}, {"section": "F", "domain": "运营 / 出单 / 客服", "id": "F1", "text": "从客户确认到出单，完整流程是什么？同一份信息需要录入几次？", "roles": ["operations"]}, {"section": "F", "domain": "运营 / 出单 / 客服", "id": "F2", "text": "你每天最常处理的五类任务是什么？每类大约占多少时间？", "roles": ["operations"]}, {"section": "F", "domain": "运营 / 出单 / 客服", "id": "F3", "text": "哪些流程同时要填 Excel 和泛微？请列出最典型的几项。", "roles": ["operations"]}, {"section": "F", "domain": "运营 / 出单 / 客服", "id": "F4", "text": "最容易出错或返工的字段、表单和流程是什么？", "roles": ["operations"]}, {"section": "F", "domain": "运营 / 出单 / 客服", "id": "F5", "text": "批改、加减人、续保、保全分别如何处理？月均处理量大约多少？", "roles": ["operations"]}, {"section": "F", "domain": "运营 / 出单 / 客服", "id": "F6", "text": "哪些工作如果自动化，会最明显地提高你的服务效率和准确率？", "roles": ["operations"]}, {"section": "G", "domain": "财务 / 佣金结算", "id": "G1", "text": "保费、佣金、发票、应收应付、付款审批目前分别在哪些系统或 Excel 中管理？", "roles": ["finance"]}, {"section": "G", "domain": "财务 / 佣金结算", "id": "G2", "text": "佣金如何计算、核对和结算？哪些环节仍依赖人工 Excel？", "roles": ["finance"]}, {"section": "G", "domain": "财务 / 佣金结算", "id": "G3", "text": "对账最常见的差异和返工原因是什么？", "roles": ["finance"]}, {"section": "G", "domain": "财务 / 佣金结算", "id": "G4", "text": "经纪人能否及时看到自己的应收佣金、已收佣金和结算状态？", "roles": ["finance"]}, {"section": "G", "domain": "财务 / 佣金结算", "id": "G5", "text": "哪些业务数据需要从泛微或其他系统二次录入财务？", "roles": ["finance"]}, {"section": "G", "domain": "财务 / 佣金结算", "id": "G6", "text": "你最希望系统自动生成哪些报表、核对结果或异常提醒？", "roles": ["finance"]}, {"section": "H", "domain": "合规 / 法务 / 内控", "id": "H1", "text": "目前哪些业务动作必须经过合规、法务或管理层审批？", "roles": ["compliance"]}, {"section": "H", "domain": "合规 / 法务 / 内控", "id": "H2", "text": "客户告知、产品推荐、授权、业务档案、佣金、投诉等关键证据目前如何留存？", "roles": ["compliance"]}, {"section": "H", "domain": "合规 / 法务 / 内控", "id": "H3", "text": "哪些岗位可以查看、导出、修改敏感客户资料？权限是否定期复核？", "roles": ["compliance"]}, {"section": "H", "domain": "合规 / 法务 / 内控", "id": "H4", "text": "管理员后台修改、数据导出、权限变更是否有审批和审计日志？", "roles": ["compliance"]}, {"section": "H", "domain": "合规 / 法务 / 内控", "id": "H5", "text": "当前最担心的三类合规风险是什么？", "roles": ["compliance"]}, {"section": "H", "domain": "合规 / 法务 / 内控", "id": "H6", "text": "哪些 AI 使用场景你认为必须设置人工审批或禁止自动执行？", "roles": ["compliance"]}, {"section": "I", "domain": "IT / 系统管理员", "id": "I1", "text": "当前主要系统有哪些？分别由谁管理、部署在哪里、年费和运维成本大约多少？", "roles": ["it"]}, {"section": "I", "domain": "IT / 系统管理员", "id": "I2", "text": "企微、泛微、财务及其他系统目前有哪些 API、数据库接口或导出能力？", "roles": ["it"]}, {"section": "I", "domain": "IT / 系统管理员", "id": "I3", "text": "泛微有哪些高频流程？哪些流程维护成本最高、二开最困难？", "roles": ["it"]}, {"section": "I", "domain": "IT / 系统管理员", "id": "I4", "text": "目前数据备份、容灾、账号管理、日志、补丁和安全策略是什么？", "roles": ["it"]}, {"section": "I", "domain": "IT / 系统管理员", "id": "I5", "text": "是否存在共享账号、离职账号未及时回收、权限过大的情况？", "roles": ["it"]}, {"section": "I", "domain": "IT / 系统管理员", "id": "I6", "text": "目前最难维护、最容易故障或最依赖供应商的系统是什么？", "roles": ["it"]}, {"section": "I", "domain": "IT / 系统管理员", "id": "I7", "text": "如果未来新增本地 AI/数据平台，现有网络、机房、NAS、VPN 和运维能力有哪些可复用？", "roles": ["it"]}], "materials": [{"id": "M01", "text": "最近三年经营数据：收入、毛利、净利润、人工成本、IT/SaaS费用、保费规模", "roles": ["owner", "finance"]}, {"id": "M02", "text": "业务结构汇总：按险种、保险公司、客户类型、经纪人、渠道", "roles": ["owner", "business_head", "finance"]}, {"id": "M03", "text": "匿名经纪人产能数据：客户数、保费、收入、佣金、新单、续保", "roles": ["owner", "business_head"]}, {"id": "M04", "text": "脱敏客户/商机数据或典型客户样本", "roles": ["business_head", "broker"]}, {"id": "M05", "text": "典型询价项目：Submission、核保问题、报价、比价表、最终选择", "roles": ["placement", "broker", "business_head"]}, {"id": "M06", "text": "典型理赔案例及案件台账（脱敏）", "roles": ["claims", "business_head"]}, {"id": "M07", "text": "投保、出单、批改、续保、保全使用的主要表单和 Excel", "roles": ["operations"]}, {"id": "M08", "text": "佣金、对账、应收应付使用的 Excel 或报表样本", "roles": ["finance"]}, {"id": "M09", "text": "企微、泛微和其他系统清单、流程清单、费用和接口情况", "roles": ["it", "owner"]}, {"id": "M10", "text": "制度：业务、佣金、合规、客户、理赔、权限、数据安全等", "roles": ["compliance", "owner"]}]}

@app.get("/health")
def health():
    try:
        with engine.connect() as con:
            con.execute(text("SELECT 1"))
        return {"ok": True, "db": "connected"}
    except Exception as e:
        return JSONResponse({"ok":False,"db":"error","detail":str(e)}, status_code=503)
