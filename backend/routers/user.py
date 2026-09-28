import json
import os
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Form, UploadFile, File
from sqlalchemy.orm import Session

from auth import get_db, get_current_user, require_user, verify_password, get_password_hash, validate_password
from config import MAX_AVATAR_SIZE
from models.schemas import ProfileUpdate
from database import User, InterviewRecord, Notification
from utils.upload_validation import (
    detect_image_type,
    extension_is_allowed,
    is_managed_avatar_url,
    safe_avatar_filename,
)

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
    # T-09 / FR-10.4：修复前这里只检查 file.content_type.startswith("image/")，
    # 而 content_type 是**客户端可任意伪造**的请求头；且完全没有大小校验，
    # 并按客户端提供的扩展名原样写入静态目录 —— 可无界写盘、可在 /uploads 下
    # 落地 .html 等可执行内容。以下四道校验全部改为服务端判定：
    #   1) 扩展名白名单（便宜的预筛）
    #   2) 大小上限（**边读边限**，避免把超大文件整个读进内存）
    #   3) 文件头魔数（真实格式，不信任 content_type）
    #   4) 统一重命名（扩展名由真实格式决定）
    if not extension_is_allowed(file.filename):
        raise HTTPException(status_code=400, detail="仅支持 PNG / JPG / GIF / WebP 图片")

    contents = await file.read(MAX_AVATAR_SIZE + 1)
    if len(contents) > MAX_AVATAR_SIZE:
        raise HTTPException(
            status_code=400,
            detail="头像文件不能超过 %dMB" % (MAX_AVATAR_SIZE // (1024 * 1024)),
        )

    detected = detect_image_type(contents)
    if detected is None:
        raise HTTPException(
            status_code=400,
            detail="文件内容不是有效的图片（仅支持 PNG / JPG / GIF / WebP）",
        )
    _mime, ext = detected

    filename = safe_avatar_filename(current_user.id, ext, uuid.uuid4().hex)
    filepath = os.path.join(UPLOAD_DIR, filename)
    with open(filepath, "wb") as buffer:
        buffer.write(contents)

    # 替换头像时清理旧文件，避免孤儿文件无界增长（仅删本服务管理的 user_* 文件）
    old_avatar = current_user.avatar
    if old_avatar and is_managed_avatar_url(old_avatar, UPLOAD_DIR):
        old_name = old_avatar[len("/uploads/avatars/"):]
        old_path = os.path.join(UPLOAD_DIR, old_name)
        if os.path.normpath(old_path) != os.path.normpath(filepath) and os.path.isfile(old_path):
            try:
                os.remove(old_path)
            except OSError:
                pass  # 清理失败不应让上传整体失败

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
    # T-08 / FR-1.5：与注册使用**同一个**校验函数，杜绝"前端 ≥6 / 后端 ≥8"的口径冲突
    password_error = validate_password(new_password)
    if password_error:
        raise HTTPException(status_code=400, detail=password_error)
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
