import type { FamilyNode } from '../../../shared/src/gateway-contract.generated'

export interface FamilyTranslations {
  title: string
  description: string
  thisChat: string
  overview: string
  refresh: string
  empty: string
  noOwner: string
  unavailable: string
  failed: string
  partial: string
  truncated: string
  open: string
  answer: string
  opening: string
  browse: string
  untitled: string
  readOnly: string
  status: Record<FamilyNode['status'], string>
}

export const familyTranslations: Record<'en' | 'fr' | 'de' | 'es' | 'ja' | 'zh' | 'zhHant' | 'ar' | 'ru', FamilyTranslations> = {
  en: {
    title: 'Family', description: 'Agent-created work stays here, not in Chats. Open a chat to read, steer or answer it.',
    thisChat: 'This chat', overview: 'Overview', refresh: 'Refresh', empty: 'No related work in this window',
    noOwner: 'The owning connection is not known yet. Reopen this chat and try again.',
    unavailable: 'Family is unavailable on this backend.', failed: 'Could not read Family',
    partial: 'Some profiles are unavailable. This is a partial view.', truncated: 'This family is larger than the current window.',
    open: 'Open chat', answer: 'Answer in chat', opening: 'Opening chat', browse: 'View family', untitled: 'Untitled work',
    readOnly: 'No chat action available',
    status: { working: 'Working', needs_you: 'Needs you', failed: 'Failed', done: 'Done', paused_by_restart: 'Paused by restart', stopped: 'Stopped', quiet: 'Quiet', unknown: 'Unknown' }
  },
  fr: {
    title: 'Famille', description: 'Le travail créé par les agents reste ici, hors des discussions. Ouvrez une discussion pour lire, guider ou répondre.',
    thisChat: 'Cette discussion', overview: 'Vue d’ensemble', refresh: 'Actualiser', empty: 'Aucun travail associé dans cette période',
    noOwner: 'La connexion propriétaire est inconnue. Rouvrez cette discussion et réessayez.',
    unavailable: 'La famille est indisponible sur ce serveur.', failed: 'Impossible de lire la famille',
    partial: 'Certains profils sont indisponibles. Cette vue est partielle.', truncated: 'Cette famille dépasse la période affichée.',
    open: 'Ouvrir la discussion', answer: 'Répondre dans la discussion', opening: 'Ouverture de la discussion', browse: 'Voir la famille', untitled: 'Travail sans titre', readOnly: 'Aucune action de discussion disponible',
    status: { working: 'En cours', needs_you: 'Votre attention', failed: 'Échec', done: 'Terminé', paused_by_restart: 'Suspendu au redémarrage', stopped: 'Arrêté', quiet: 'Calme', unknown: 'Inconnu' }
  },
  de: {
    title: 'Familie', description: 'Von Agenten erstellte Arbeit bleibt hier, nicht in Chats. Öffne einen Chat zum Lesen, Steuern oder Antworten.',
    thisChat: 'Dieser Chat', overview: 'Übersicht', refresh: 'Aktualisieren', empty: 'Keine zugehörige Arbeit in diesem Zeitraum',
    noOwner: 'Die zuständige Verbindung ist noch unbekannt. Öffne diesen Chat erneut.',
    unavailable: 'Familie ist auf diesem Backend nicht verfügbar.', failed: 'Familie konnte nicht gelesen werden',
    partial: 'Einige Profile sind nicht verfügbar. Diese Ansicht ist unvollständig.', truncated: 'Diese Familie ist größer als der aktuelle Ausschnitt.',
    open: 'Chat öffnen', answer: 'Im Chat antworten', opening: 'Chat wird geöffnet', browse: 'Familie ansehen', untitled: 'Unbenannte Arbeit', readOnly: 'Keine Chat-Aktion verfügbar',
    status: { working: 'In Arbeit', needs_you: 'Braucht dich', failed: 'Fehlgeschlagen', done: 'Fertig', paused_by_restart: 'Durch Neustart pausiert', stopped: 'Gestoppt', quiet: 'Ruhig', unknown: 'Unbekannt' }
  },
  es: {
    title: 'Familia', description: 'El trabajo creado por agentes permanece aquí, no en Chats. Abre un chat para leer, orientar o responder.',
    thisChat: 'Este chat', overview: 'Resumen', refresh: 'Actualizar', empty: 'No hay trabajo relacionado en este periodo',
    noOwner: 'Aún se desconoce la conexión propietaria. Vuelve a abrir este chat.',
    unavailable: 'Familia no está disponible en este servidor.', failed: 'No se pudo leer Familia',
    partial: 'Algunos perfiles no están disponibles. Esta vista es parcial.', truncated: 'Esta familia supera la ventana actual.',
    open: 'Abrir chat', answer: 'Responder en el chat', opening: 'Abriendo chat', browse: 'Ver familia', untitled: 'Trabajo sin título', readOnly: 'No hay acciones de chat disponibles',
    status: { working: 'Trabajando', needs_you: 'Te necesita', failed: 'Falló', done: 'Terminado', paused_by_restart: 'Pausado por reinicio', stopped: 'Detenido', quiet: 'Sin actividad', unknown: 'Desconocido' }
  },
  ja: {
    title: 'ファミリー', description: 'エージェントが作成した作業はチャット一覧ではなくここに表示されます。チャットを開いて閲覧・指示・回答できます。',
    thisChat: 'このチャット', overview: '概要', refresh: '更新', empty: 'この期間に関連する作業はありません', noOwner: '所有接続が不明です。チャットを開き直してください。',
    unavailable: 'このバックエンドではファミリーを利用できません。', failed: 'ファミリーを読み取れませんでした', partial: '一部のプロファイルが利用できないため、部分的な表示です。', truncated: 'このファミリーは現在の表示範囲を超えています。',
    open: 'チャットを開く', answer: 'チャットで回答', opening: 'チャットを開いています', browse: 'ファミリーを見る', untitled: '無題の作業', readOnly: '利用できるチャット操作はありません',
    status: { working: '作業中', needs_you: '確認が必要', failed: '失敗', done: '完了', paused_by_restart: '再起動で一時停止', stopped: '停止', quiet: '静止中', unknown: '不明' }
  },
  zh: {
    title: '工作家族', description: '代理创建的工作保留在这里，不进入聊天列表。打开聊天即可阅读、指导或回答。',
    thisChat: '此聊天', overview: '概览', refresh: '刷新', empty: '此时间范围内没有相关工作', noOwner: '尚不清楚所属连接。请重新打开此聊天。',
    unavailable: '此后端不支持工作家族。', failed: '无法读取工作家族', partial: '部分配置不可用，此视图不完整。', truncated: '此家族超出当前显示范围。',
    open: '打开聊天', answer: '在聊天中回答', opening: '正在打开聊天', browse: '查看家族', untitled: '未命名工作', readOnly: '无可用聊天操作',
    status: { working: '工作中', needs_you: '需要你', failed: '失败', done: '完成', paused_by_restart: '因重启暂停', stopped: '已停止', quiet: '暂无活动', unknown: '未知' }
  },
  zhHant: {
    title: '工作家族', description: '代理建立的工作保留在這裡，不進入聊天列表。開啟聊天即可閱讀、指導或回答。',
    thisChat: '此聊天', overview: '總覽', refresh: '重新整理', empty: '此時間範圍內沒有相關工作', noOwner: '尚不清楚所屬連線。請重新開啟此聊天。',
    unavailable: '此後端不支援工作家族。', failed: '無法讀取工作家族', partial: '部分設定檔無法使用，此檢視不完整。', truncated: '此家族超出目前顯示範圍。',
    open: '開啟聊天', answer: '在聊天中回答', opening: '正在開啟聊天', browse: '檢視家族', untitled: '未命名工作', readOnly: '無可用聊天操作',
    status: { working: '工作中', needs_you: '需要你', failed: '失敗', done: '完成', paused_by_restart: '因重新啟動暫停', stopped: '已停止', quiet: '暫無活動', unknown: '未知' }
  },
  ar: {
    title: 'العائلة', description: 'يبقى عمل الوكلاء هنا، خارج قائمة المحادثات. افتح محادثة للقراءة أو التوجيه أو الإجابة.',
    thisChat: 'هذه المحادثة', overview: 'نظرة عامة', refresh: 'تحديث', empty: 'لا عمل مرتبط ضمن هذه الفترة', noOwner: 'الاتصال المالك غير معروف بعد. أعد فتح المحادثة.',
    unavailable: 'العائلة غير متاحة على هذا الخادم.', failed: 'تعذرت قراءة العائلة', partial: 'بعض الملفات الشخصية غير متاحة. هذه نظرة جزئية.', truncated: 'تتجاوز هذه العائلة نطاق العرض الحالي.',
    open: 'فتح المحادثة', answer: 'الإجابة في المحادثة', opening: 'جارٍ فتح المحادثة', browse: 'عرض العائلة', untitled: 'عمل بلا عنوان', readOnly: 'لا إجراء محادثة متاح',
    status: { working: 'يعمل', needs_you: 'بحاجة إليك', failed: 'فشل', done: 'مكتمل', paused_by_restart: 'متوقف بسبب إعادة التشغيل', stopped: 'متوقف', quiet: 'هادئ', unknown: 'غير معروف' }
  },
  ru: {
    title: 'Семья', description: 'Работа агентов остаётся здесь, вне списка чатов. Откройте чат, чтобы прочитать, направить или ответить.',
    thisChat: 'Этот чат', overview: 'Обзор', refresh: 'Обновить', empty: 'Нет связанной работы за этот период', noOwner: 'Соединение владельца пока неизвестно. Откройте чат заново.',
    unavailable: 'Семья недоступна на этом сервере.', failed: 'Не удалось прочитать семью', partial: 'Некоторые профили недоступны. Обзор неполный.', truncated: 'Эта семья больше текущего окна.',
    open: 'Открыть чат', answer: 'Ответить в чате', opening: 'Открытие чата', browse: 'Посмотреть семью', untitled: 'Работа без названия', readOnly: 'Нет доступных действий чата',
    status: { working: 'В работе', needs_you: 'Нужно ваше внимание', failed: 'Ошибка', done: 'Готово', paused_by_restart: 'Приостановлено перезапуском', stopped: 'Остановлено', quiet: 'Нет активности', unknown: 'Неизвестно' }
  }
}
