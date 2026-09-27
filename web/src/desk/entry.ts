import { topicPath } from "./api";

/**
 * Where a topic's screens are, and which one the study desk opens (#368, epic #365).
 *
 * `topicEntryPath` is the single place that decides where clicking a topic's name on the desk
 * lands. Default (open product question of #368): **Construir**, the study workspace
 * (`.../workspace`). If the answer changes (Estudiar when the topic has a study version, or the
 * topic card page), only this function changes; every caller keeps passing the topic.
 */

/** The ids a topic's paths are built from (a protocol `Topic` carries them). */
export interface TopicRef {
  subject_id: string;
  topic_id: string;
}

/** Construir: the study workspace (`/subjects/<s>/topics/<t>/workspace`). */
export function topicWorkspacePath(topic: TopicRef): string {
  return `${topicPath(topic.subject_id, topic.topic_id)}/workspace`;
}

/** Estudiar: the study screen (`/subjects/<s>/topics/<t>/study`). */
export function topicStudyPath(topic: TopicRef): string {
  return `${topicPath(topic.subject_id, topic.topic_id)}/study`;
}

/** Ficha: the topic card page (`/subjects/<s>/topics/<t>`). */
export function topicCardPath(topic: TopicRef): string {
  return topicPath(topic.subject_id, topic.topic_id);
}

/** Where the desk opens a topic: Construir (the workspace) until the product question says otherwise. */
export function topicEntryPath(topic: TopicRef): string {
  return topicWorkspacePath(topic);
}
