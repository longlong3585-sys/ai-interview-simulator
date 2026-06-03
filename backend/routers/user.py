import json
import os
import shutil
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Form, UploadFile, File
from sqlalchemy.orm import Session

from auth import get_db, get_current_user, require_user, verify_password, get_password_hash
from models.schemas import ProfileUpdate
from database import User, InterviewRecord, Notification

router = APIRouter(prefix="/api", tags=["user"])

UPLOAD_DIR = "uploads/avatars"
os.makedirs(UPLOAD_DIR, exist_ok=True)


@router.get("/user/profile")
def get_profile(current_user: User = Depends(get_current_user)):
    return {
        "username": current_user.username,
        "nickname": current_user.nickname,
        "avatar": current_user.avatar,
        "bio": current_user.bio,
        "gender": current_user.gender,
        "birthday": current_user.birthday.isoformat() if current_user.birthday else None,
        "email": current_user.email
    }


@router.patch("/user/profile")
def update_profile(
    profile: ProfileUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if profile.nickname is not None:
        current_user.nickname = profile.nickname
    if profile.bio is not None:
        current_user.bio = profile.bio
    if profile.gender is not None:
        if profile.gender not in ['male', 'female', 'other']:
            raise HTTPException(status_code=400, detail="无效的性别")
        current_user.gender = profile.gender
    if profile.birthday is not None:
        current_user.birthday = profile.birthday
    db.commit()
    return {"msg": "更新成功"}


@router.post("/user/avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="只支持图片文件")
    ext = os.path.splitext(file.filename)[1] if file.filename else ".jpg"
    filename = f"user_{current_user.id}_{uuid.uuid4().hex}{ext}"
    filepath = os.path.join(UPLOAD_DIR, filename)
    with open(filepath, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    avatar_url = f"/uploads/avatars/{filename}"
    current_user.avatar = avatar_url
    db.commit()
    return {"avatar_url": avatar_url}


@router.get("/user/stats")
def get_user_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    records = db.query(InterviewRecord).filter(InterviewRecord.user_id == current_user.id).all()
    total = len(records)
    scores = []
    for r in records:
        try:
            report = json.loads(r.report)
            if 'overall_score' in report:
                scores.append(float(report['overall_score']))
        except:
            pass
    avg = sum(scores) / len(scores) if scores else 0
    return {"total_interviews": total, "avg_score": round(avg, 1)}


@router.post("/change_password")
def change_password(
    old_password: str = Form(...),
    new_password: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if not verify_password(old_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="原密码错误")
    if len(new_password) < 8:
        raise HTTPException(status_code=400, detail="新密码长度至少8位")
    current_user.hashed_password = get_password_hash(new_password)
    db.commit()
    return {"msg": "密码修改成功"}


@router.get("/history")
def get_history(db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    records = db.query(InterviewRecord).filter(InterviewRecord.user_id == current_user.id).order_by(InterviewRecord.created_at.desc()).all()
    return [{"id": r.id, "role": r.role, "created_at": r.created_at.isoformat(), "report": json.loads(r.report), "status": r.status, "admin_comment": r.admin_comment} for r in records]


@router.get("/history_item/{record_id}")
def get_history_item(record_id: int, db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    record = db.query(InterviewRecord).filter(InterviewRecord.id == record_id, InterviewRecord.user_id == current_user.id).first()
    if not record:
        raise HTTPException(status_code=404, detail="该面试记录已不存在")
    return {"id": record.id, "role": record.role, "created_at": record.created_at.isoformat(), "report": json.loads(record.report), "status": record.status, "admin_comment": record.admin_comment}


@router.get("/notifications")
def get_notifications(db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    notifications = db.query(Notification).filter(Notification.user_id == current_user.id).order_by(Notification.created_at.desc()).all()
    return [{"id": n.id, "type": n.type, "message": n.message, "is_read": n.is_read, "target_type": n.target_type, "target_id": n.target_id, "created_at": n.created_at.isoformat()} for n in notifications]


@router.get("/notifications/unread_count")
def get_unread_count(db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    count = db.query(Notification).filter(Notification.user_id == current_user.id, Notification.is_read == False).count()
    return {"count": count}


@router.patch("/notifications/{notification_id}/read")
def mark_notification_read(notification_id: int, db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    notification = db.query(Notification).filter(Notification.id == notification_id, Notification.user_id == current_user.id).first()
    if not notification:
        raise HTTPException(status_code=404, detail="通知不存在")
    notification.is_read = True
    db.commit()
    return {"msg": "已标记为已读"}


@router.patch("/notifications/read_all")
def mark_all_read(db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    db.query(Notification).filter(Notification.user_id == current_user.id, Notification.is_read == False).update({Notification.is_read: True})
    db.commit()
    return {"msg": "已全部标记为已读"}


@router.delete("/notifications/{notification_id}")
def delete_notification(notification_id: int, db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    notification = db.query(Notification).filter(Notification.id == notification_id, Notification.user_id == current_user.id).first()
    if not notification:
        raise HTTPException(status_code=404, detail="通知不存在")
    db.delete(notification)
    db.commit()
    return {"msg": "已删除"}


@router.delete("/notifications/clear_all")
def clear_all_notifications(db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    db.query(Notification).filter(Notification.user_id == current_user.id).delete()
    db.commit()
    return {"msg": "已清空全部通知"}
