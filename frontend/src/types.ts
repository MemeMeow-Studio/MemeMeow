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
}

export interface ImageMetadataSummary {
  status?: string
}

export interface MemeImage {
  meme_id: string
  filename: string
  media_url: string
  size?: number
  extension?: string
  metadata?: ImageMetadataSummary
  embedding_status?: string
  visual_embedding_status?: string
}

export interface CollectionSummary {
  collection_id: string
  name: string
  member_count?: number
  cover_media_url?: string
  members?: MemeImage[]
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
  image_stage?: 'visual' | 'agent' | 'text_embedding' | null
  processing_job_id?: string | null
  historical_unclassified?: boolean
  read_only?: boolean
  retry_allowed?: boolean
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
  stage: 'visual' | 'agent' | 'text_embedding'
  status: string
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
  status: string
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
}

export interface ClipboardNotice {
  message: string
  requestId: number
}
