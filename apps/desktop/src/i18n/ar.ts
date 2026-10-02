import { arArtifacts } from './ar_artifacts'
import { arAssistant } from './ar_assistant'
import { arBoot } from './ar_boot'
import { arCapabilities } from './ar_capabilities'
import { arChat } from './ar_chat'
import { arChrome } from './ar_chrome'
import { arCommandCenter } from './ar_command_center'
import { arCommon } from './ar_common'
import { arConnectors } from './ar_connectors'
import { arDiagnostics } from './ar_diagnostics'
import { arSettings } from './ar_settings'
import { defineLocale } from './define-locale'

export const ar = defineLocale({
  sharedMetrics: arCommon.sharedMetrics,
  externalOpenFailed: arChrome.externalOpenFailed,
  catalog: arCapabilities.catalog,
  sessionImport: arConnectors.sessionImport,
  sendDiagnostics: arDiagnostics.sendDiagnostics,
  common: arCommon.common,
  fileMenu: arChrome.fileMenu,
  boot: arBoot.boot,
  notifications: arDiagnostics.notifications,
  remoteDisplayBanner: arBoot.remoteDisplayBanner,
  titlebar: arChrome.titlebar,
  keybinds: arChrome.keybinds,
  language: arSettings.language,
  settings: arSettings.settings,
  skills: arCapabilities.skills,
  agents: arCapabilities.agents,
  commandCenter: arCommandCenter.commandCenter,
  messaging: arCommandCenter.messaging,
  profiles: arCommandCenter.profiles,
  modelAssignment: arSettings.modelAssignment,
  cron: arCommandCenter.cron,
  artifacts: arArtifacts.artifacts,
  artifactCard: arArtifacts.artifactCard,
  artifactPreview: arArtifacts.artifactPreview,
  sidebar: arChrome.sidebar,
  composer: arChat.composer,
  statusStack: arChat.statusStack,
  updates: {
    ...arBoot.updates,
    codexUpdateAction: 'التحديث عبر Codex',
    codexUpdateHint: 'ينسخ طلب تحديث للصقه في Codex. لا يتم إرسال أو تثبيت أي شيء من هنا.',
    codexUpdateCopied: 'تم نسخ الطلب. الصقه في Codex للمتابعة؛ لم يبدأ أي تحديث.',
    managedTitle: 'تحديثات Hermes',
    managedSubtitle: 'اطّلع على الجديد، ثم جهّز التحديث مع Codex.',
    desktopUpstreamTitle: 'المنبع الرسمي لتطبيق سطح المكتب',
    desktopUpstreamSubtitle: 'مقارنة للقراءة فقط بين التطبيق المثبّت و NousResearch/main.',
    desktopUpstreamReady: 'حالة المنبع الرسمي محدّثة.',
    desktopUpstreamStale: 'يتم عرض حالة مخزنة مؤقتًا للمنبع الرسمي.',
    desktopUpstreamError: 'حالة المنبع الرسمي غير متاحة.',
    desktopUpstreamUnavailable: 'لا تتوفر هوية موثوقة لحزمة التطبيق المثبّتة.',
    desktopUpstreamDirty: 'بُني هذا التطبيق من مصدر معدّل؛ لا يشمل العدد تلك التعديلات المحلية.',
    desktopUpstreamBehind: 'متأخر عن المنبع الرسمي',
    desktopUpstreamAhead: 'التزامات خاصة بالتفرع',
    desktopUpstreamInstalled: 'التطبيق المثبّت',
    desktopUpstreamOfficial: 'NousResearch/main',
    desktopUpstreamReadOnlyNotice:
      'للقراءة فقط: لا يعيد هذا الفحص ضبط المستودع أو يطبّق تحديثًا أو يزيل تعديلات CloudSeed.',
    managedReady: 'حالة مراقب المصدر حديثة.',
    managedStale: 'حالة مراقب المصدر قديمة.',
    managedMissing: 'حالة مراقب المصدر غير متاحة.',
    managedInvalid: 'حالة مراقب المصدر غير صالحة.',
    managedUnreadable: 'تعذرت قراءة حالة مراقب المصدر.',
    managedRunningRelease: 'الإصدار الجاري',
    managedUpstream: 'المصدر الأعلى',
    managedCommitsBehind: count => `${count} التزام متأخر عن المصدر الأعلى`,
    managedCandidate: 'المرشح',
    managedCandidateStatuses: {
      blocked: 'محظور',
      building: 'قيد البناء',
      current: 'محدّث',
      not_built: 'لم يُبنَ',
      passed: 'نجح',
      ready: 'جاهز'
    },
    managedLocalPatches: 'تراكبات CloudSeed',
    managedSourceRefs: 'مراجع المصدر',
    managedReachable: 'يمكن الوصول إليها',
    managedNotReachable: 'لا يمكن الوصول إليها',
    managedUnknown: 'غير معروف',
    managedBlockers: 'العوائق',
    managedNextAction: 'الإجراء التالي',
    managedRefreshRequested: 'طُلب تحديث مصدر للقراءة فقط.',
    managedCheckNow: 'تحقق الآن',
    managedBuildCandidate: 'بناء مرشح',
    managedRequestingCandidate: 'جار الطلب…',
    managedRequestOnlyNotice: 'هذه الأزرار ترسل طلبات فقط. لا يتم تغيير الإنتاج أو إعادة تشغيله.',
    backend: {
      ...arBoot.updates.backend,
      candidateRequested: 'طُلب بناء مرشح غير قابل للتغيير. لم يتغير الإنتاج.'
    }
  },
  guidedGreeting: arBoot.guidedGreeting,
  install: arBoot.install,
  onboarding: arBoot.onboarding,
  modelPicker: arSettings.modelPicker,
  modelVisibility: arSettings.modelVisibility,
  shell: arChrome.shell,
  rightSidebar: arChrome.rightSidebar,
  preview: arArtifacts.preview,
  interfaceMode: arSettings.interfaceMode,
  zones: arChrome.zones,
  contextMenu: arChrome.contextMenu,
  assistant: arAssistant.assistant,
  prompts: arChat.prompts,
  desktop: arChat.desktop,
  errors: arDiagnostics.errors,
  tips: arChat.tips,
  ui: arCommon.ui
})
