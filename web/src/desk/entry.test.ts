import { expect, it } from "vitest";
import { topicCardPath, topicStudyPath, topicWorkspacePath } from "./entry";

const topic = { subject_id: "historia", topic_id: "revolución francesa" };

it("builds the workspace path with quoted ids", () => {
  expect(topicWorkspacePath(topic)).toBe("/subjects/historia/topics/revoluci%C3%B3n%20francesa/workspace");
});

it("builds the study screen and topic card paths with quoted ids", () => {
  expect(topicStudyPath(topic)).toBe("/subjects/historia/topics/revoluci%C3%B3n%20francesa/study");
  expect(topicCardPath({ subject_id: "a/b", topic_id: "t" })).toBe("/subjects/a%2Fb/topics/t");
});
