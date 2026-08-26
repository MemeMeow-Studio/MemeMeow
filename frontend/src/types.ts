/**
 * 前端工作台的共享领域类型，统一组件契约与后端响应的最小稳定形状。
 */

export type PageId = 'search' | 'library' | 'collections' | 'upload' | 'tasks'

export interface NavigationItem {
  id: PageId
  label: string
}

export interface ServiceConfig {
  embedding_model?: string
  embedding_cache_ready?: boolean
  reverse_image_available?: boolean
  /** 服务端强制的单请求文件数上限。 */
  max_files_per_request?: number
  /** 仅供客户端调度器使用的并发提示，不代表服务端 admission 限制。 */
  max_concurrent_upload_requests?: number
  /** 服务端可选的单请求文件字节预算；null 表示 disabled。 */
  max_request_bytes?: number | null
}

export interface ImageProcessingOptions {
  reverse_image_policy: 'forbid' | 'auto'
  auto_name: boolean
}

/** 图片库批量重试对话框可选择的三个核心处理阶段。 */
export type CoreImageProcessingStage = 'visual' | 'agent' | 'text_embedding'

/** 图片库批量重试对话框的提交模式。 */
export type SelectedImageRetryMode = 'full' | 'parts'

/** 图片库按阶段批量提交请求的稳定前端形状。 */
export interface SelectedImageStageRetryRequest {
  items: Array<{ meme_id: string }>
  stages: CoreImageProcessingStage[]
}

export interface ImageMetadataSummary {
  status?: string
}

/** 缩略图派生事实的有限状态，只有 available 可以携带媒体地址。 */
export type ThumbnailStatus = 'available' | 'pending' | 'failed' | 'stale'

/** 与同一 Meme 原图版本绑定的受控缩略图投影。 */
export interface ThumbnailInfo {
  status: ThumbnailStatus
  media_url?: string | null
  width?: number
  height?: number
  media_type?: string
}

export interface MemeImage {
  meme_id: string
  filename: string
  media_url: string
  thumbnail?: ThumbnailInfo
  size?: number
  extension?: string
  metadata?: ImageMetadataSummary
  embedding_status?: string
  visual_embedding_status?: string
  processing_job_id?: string
  processing_status?: string
  processing_auto_name?: boolean
  processing_has_warnings?: boolean
  processing_stages?: ImageProcessingStage[]
}

export interface CollectionSummary {
  collection_id: string
  name: string
  member_count?: number
  cover_media_url?: string
  cover_meme_id?: string
  cover_thumbnail?: ThumbnailInfo
  members?: MemeImage[]
}

/** 保持检索 results 数组兼容的原图/缩略图旁路关联。 */
export interface SearchResultMedia {
  meme_id: string
  media_url: string
  thumbnail?: ThumbnailInfo
}

/** 检索响应的稳定主结果和可选富媒体旁路。 */
export interface SearchResponse {
  results: string[]
  result_media?: SearchResultMedia[]
}

export interface AgentActivityView {
  turns: string
  lastActivity: string
  ariaLabel: string
}

export interface TaskItem {
  task_id: string
  task_type: string
  submission_mode?: 'pipeline' | 'standalone' | null
  image_stage?: 'visual' | 'agent' | 'auto_rename' | 'text_embedding' | null
  processing_job_id?: string | null
  historical_unclassified?: boolean
  read_only?: boolean
  retry_allowed?: boolean
  image_stage_recoverable?: boolean
  image_stage_status?: string | null
  status: string
  progress?: number | null
  message?: string
  created_at?: string
  updated_at?: string
  completed_at?: string
  image?: Pick<MemeImage, 'meme_id' | 'filename'>
  error?: { error?: string; message?: string }
  result?: {
    auto_named?: boolean
    saved_filename?: string
    auto_name_error?: string
  }
  agent_completed_turns?: number
  agent_turn_running?: boolean
  agent_last_activity_at?: string
}

export interface ImageProcessingStage {
  stage: 'visual' | 'agent' | 'auto_rename' | 'text_embedding'
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'blocked' | 'unknown_execution' | 'skipped' | 'warning'
  task_id?: string | null
  attempt?: number
  error?: { error?: string; message?: string } | null
  retry_at?: string | null
  submission_mode?: 'pipeline'
  processing_job_id?: string
}

export interface ImageProcessingJob {
  task_id: string
  task_type: 'image_processing'
  job_id: string
  meme_id: string
  submission_mode: 'pipeline'
  image_stage?: null
  processing_job_id: string
  revision: number
  image_sha256: string
  reverse_image_policy: string
  auto_name?: boolean
  status: string
  has_warnings?: boolean
  warnings?: Array<{ stage?: string; error?: string; message?: string; recoverable?: boolean }>
  current_stage?: string | null
  stages: ImageProcessingStage[]
  error?: { error?: string; message?: string } | null
  progress?: number | null
  message?: string | null
  created_at?: string
  updated_at?: string
  completed_at?: string | null
}

export interface UploadResult {
  meme_id?: string
  filename: string
  ok: boolean
  metadata_job_id?: string
  saved_filename?: string
  error?: string
  processing_job_id?: string
  auto_name?: boolean
  reverse_image_policy?: 'forbid' | 'auto'
  idempotent?: boolean
  processing_status?: string
  processing_progress?: number | null
  processing_message?: string | null
  metadata_status?: string
}

export interface UnreadyProcessingResponse {
  target_count: number
  submitted_count: number
  reused_count: number
  conflict_count: number
  failed_count: number
  results: Array<{ meme_id: string; processing_job_id?: string; status?: string; reused?: boolean; error?: string; category?: 'submitted' | 'reused' | 'conflict' | 'failed' }>
}

/** 图片库按阶段批量提交响应的最小稳定形状。 */
export interface SelectedImageStageRetryResponse {
  target_count: number
  submitted_count: number
  failed_count: number
  results: Array<{
    meme_id: string
    stage: CoreImageProcessingStage
    task_id?: string
    status?: string
    error?: string
  }>
}

export interface ClipboardNotice {
  message: string
  requestId: number
}
