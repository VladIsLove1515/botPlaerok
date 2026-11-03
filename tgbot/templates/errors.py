import textwrap
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from error_cases import open_cases, find_case

from .. import callback_datas as calls


def errors_text():
    cases = open_cases()
    if not cases:
        return textwrap.dedent("""
            <b>❗ Ошибки</b>
            <blockquote>Пока нет активных жалоб. Когда покупатель напишет <b>!ошибка</b> или <b>ошибка</b>, здесь появится кнопка с его ником.</blockquote>
        """)
    return textwrap.dedent(f"""
        <b>❗ Ошибки</b>
        Активных жалоб: <b>{len(cases)}</b>
        <blockquote>Нажмите на ник — откроется решение. Сейчас можно выдать доп. аккаунт из дорогой очереди.</blockquote>
    """)


def errors_kb():
    rows = []
    for case in open_cases()[:20]:
        username = str(case.get("username") or "клиент")
        rows.append([InlineKeyboardButton(
            text=f"👤 {username}"[:64],
            callback_data=calls.ErrorCasePage(id=str(case.get("id") or "")).pack()
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=calls.MenuNavigation(to="default").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def error_case_text(case_id: str):
    case = find_case(case_id)
    if not case:
        return "<b>❗ Ошибка</b>\n\nЖалоба не найдена или уже закрыта."
    username = case.get("username") or "клиент"
    chat_id = case.get("chat_id") or ""
    item = case.get("item_name") or "—"
    created = case.get("created_at") or "—"
    status = "открыта" if case.get("status") == "open" else "закрыта"
    return textwrap.dedent(f"""
        <b>❗ Ошибка · {username}</b>
        <b>Статус:</b> {status}
        <b>Чат:</b> <a href="https://playerok.com/chats/{chat_id}">открыть</a>
        <b>Товар:</b> {item}
        <b>Время:</b> {created}
        <blockquote>Пока одно решение — выдать доп. аккаунт из <b>дорогой</b> очереди. Раздел будем расширять.</blockquote>
    """)


def error_case_kb(case_id: str):
    case = find_case(case_id)
    rows = []
    if case and case.get("status") == "open":
        rows.append([InlineKeyboardButton(
            text="🎁 Выдать доп аккаунт",
            callback_data=calls.IssueExtraAccount(id=case_id).pack()
        )])
        chat_id = str(case.get("chat_id") or "")
        if chat_id:
            rows.append([InlineKeyboardButton(
                text="💬 Диалог",
                callback_data=calls.ChatPage(id=chat_id).pack()
            )])
    rows.append([InlineKeyboardButton(text="⬅️ К ошибкам", callback_data=calls.MenuNavigation(to="errors").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def error_case_float_text(placeholder: str):
    return textwrap.dedent(f"""
        <b>❗ Ошибка</b>
        \n{placeholder}
    """)
