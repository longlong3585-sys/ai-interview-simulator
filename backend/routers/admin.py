import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Form
from sqlalchemy.orm import Session
from sqlalchemy import func

from auth import get_db, get_current_admin_user, get_password_hash
from models.schemas import InterviewUpdateRequest
from database import User, InterviewRecord, Notification

router = APIRouter(prefix="/api/admin", tags=["admin"])

NOTIFICATION_TEMPLATES = {
    "interview_approved": {
        "message": "恭喜你！你的{role}岗位面试已通过，请联系HR办理入职手续。",
    },
    "interview_rejected": {
        "message": "很遗憾，你的{role}岗位面试未通过，继续加油，下次一定行！",
    },
    "interview_pending": {
        "message": "你的{role}岗位面试状态已变更为待审核，管理员正在评估中。",
    },
    "new_comment": {
        "message": "管理员对你的{role}面试添加了评语，快去看看吧。",
    },
}


def create_notification(db: Session, user_id: int, ntype: str, role: str = "", interview_id: int = 0):
    template = NOTIFICATION_TEMPLATES.get(ntype)
    if not template:
        return None
    message = template["message"].format(role=role)

    target_type = "interview_record" if interview_id else None
    target_id = interview_id if interview_id else None

    existing = db.query(Notification).filter(
        Notification.user_id == user_id,
        Notification.type == ntype,
        Notification.is_read == False,
        Notification.created_at >= datetime.utcnow() - timedelta(minutes=5)
    ).first()
    if existing:
        existing.message = message
        existing.target_type = target_type
        existing.target_id = target_id
        existing.created_at = datetime.utcnow()
        return existing

    notification = Notification(
        user_id=user_id,
        type=ntype,
        message=message,
        target_type=target_type,
        target_id=target_id,
        is_read=False
    )
    db.add(notification)
    return notification


@router.get("/users")
def admin_get_users(db: Session = Depends(get_db), current_user: User = Depends(get_current_admin_user)):
    users = db.query(User).all()
    return [{"id": u.id, "username": u.username, "role": u.role, "email": u.email, "is_active": u.is_active, "created_at": u.created_at.isoformat()} for u in users]


@router.get("/interviews")
def admin_get_interviews(db: Session = Depends(get_db), current_user: User = Depends(get_current_admin_user)):
    records = db.query(InterviewRecord).order_by(InterviewRecord.created_at.desc()).all()
    return [{"id": r.id, "user_id": r.user_id, "username": r.user.username if r.user else "未知", "role": r.role, "status": r.status, "admin_comment": r.admin_comment, "report": json.loads(r.report) if r.report else None, "created_at": r.created_at.isoformat()} for r in records]


@router.patch("/interviews/{interview_id}")
def admin_update_interview(
    interview_id: int,
    req: InterviewUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user)
):
    record = db.query(InterviewRecord).filter(InterviewRecord.id == interview_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="记录不存在")
    old_status = record.status
    old_comment = record.admin_comment
    if req.status:
        if req.status not in ["pending", "approved", "rejected"]:
            raise HTTPException(status_code=400, detail="无效的状态")
        record.status = req.status
    if req.admin_comment is not None:
        record.admin_comment = req.admin_comment
    db.commit()

    if req.status and old_status != req.status:
        if req.status == "approved":
            create_notification(db, record.user_id, "interview_approved", role=record.role, interview_id=interview_id)
        elif req.status == "rejected":
            create_notification(db, record.user_id, "interview_rejected", role=record.role, interview_id=interview_id)
        elif req.status == "pending":
            create_notification(db, record.user_id, "interview_pending", role=record.role, interview_id=interview_id)
        db.commit()
    if req.admin_comment is not None and req.admin_comment != old_comment:
        create_notification(db, record.user_id, "new_comment", role=record.role, interview_id=interview_id)
        db.commit()

    return {"msg": "更新成功"}


@router.delete("/interviews/{interview_id}")
def admin_delete_interview(
    interview_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user)
):
    record = db.query(InterviewRecord).filter(InterviewRecord.id == interview_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="记录不存在")
    db.delete(record)
    db.commit()
    return {"msg": "面试记录已删除"}


@router.post("/users/{user_id}/reset_password")
def admin_reset_password(
    user_id: int,
    new_password: str = Form(...),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin_user)
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if len(new_password) < 8:
        raise HTTPException(status_code=400, detail="新密码长度至少8位")
    user.hashed_password = get_password_hash(new_password)
    db.commit()
    return {"msg": "密码重置成功"}


@router.patch("/users/{user_id}/toggle_active")
def toggle_user_active(
    user_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin_user)
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.role == "admin":
        raise HTTPException(status_code=403, detail="不能禁用管理员账号")
    user.is_active = not user.is_active
    db.commit()
    return {"msg": f"用户已{'启用' if user.is_active else '禁用'}"}


@router.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin_user)
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.role == "admin":
        raise HTTPException(status_code=403, detail="不能删除管理员账号")
    db.query(InterviewRecord).filter(InterviewRecord.user_id == user_id).delete()
    db.query(Notification).filter(Notification.user_id == user_id).delete()
    db.delete(user)
    db.commit()
    return {"msg": "用户已删除"}


@router.get("/stats")
def get_admin_stats(db: Session = Depends(get_db), _: User = Depends(get_current_admin_user)):
    total_users = db.query(User).filter(User.is_active == True).count()
    total_interviews = db.query(InterviewRecord).count()

    all_reports = db.query(InterviewRecord.report).filter(InterviewRecord.report.isnot(None)).all()
    scores = []
    for (r,) in all_reports:
        try:
            report_json = json.loads(r)
            if 'overall_score' in report_json:
                scores.append(float(report_json['overall_score']))
        except:
            pass
    avg_score = sum(scores) / len(scores) if scores else 0

    approved_count = db.query(InterviewRecord).filter(InterviewRecord.status == 'approved').count()
    pass_rate = (approved_count / total_interviews * 100) if total_interviews > 0 else 0

    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    daily_counts = db.query(
        func.date(InterviewRecord.created_at).label('date'),
        func.count().label('count')
    ).filter(InterviewRecord.created_at >= seven_days_ago).group_by(func.date(InterviewRecord.created_at)).all()

    daily_chart = [{"date": str(row.date), "count": row.count} for row in daily_counts]

    return {
        "total_users": total_users,
        "total_interviews": total_interviews,
        "avg_overall_score": round(avg_score, 1),
        "pass_rate": round(pass_rate, 1),
        "daily_interviews": daily_chart
    }
