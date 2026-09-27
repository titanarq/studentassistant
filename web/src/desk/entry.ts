import { topicPath } from "./api";

/**
 * Where a topic's screens are (#368, epic #365). The desk never picks one for the student: each
 * topic row offers **Construir** and **Estudiar** explicitly, and its name leads to the topic card.
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
