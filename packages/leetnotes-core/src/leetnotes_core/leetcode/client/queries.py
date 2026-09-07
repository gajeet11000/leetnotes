QUESTION_DETAILS_QUERY = """
query selectQuestion($titleSlug: String!) {
  question(titleSlug: $titleSlug) {
    content
    questionFrontendId
    title
    titleSlug
    difficulty
    categoryTitle
    topicTags {
      name
      slug
    }
  }
}
"""

SUBMISSION_LIST_QUERY = """
query submissionList($questionSlug: String!, $limit: Int, $offset: Int) {
  questionSubmissionList(
    questionSlug: $questionSlug
    limit: $limit
    offset: $offset
  ) {
    submissions {
      id
      statusDisplay
      lang
      timestamp
    }
  }
}
"""

RECENT_AC_SUBMISSIONS_QUERY = """
query recentAcSubmissions($username: String!, $limit: Int!) {
  recentAcSubmissionList(username: $username, limit: $limit) {
    title
    titleSlug
    timestamp
  }
}
"""

SUBMISSION_DETAILS_QUERY = """
query submissionDetails($submissionId: Int!) {
  submissionDetails(submissionId: $submissionId) {
    id
    code
    timestamp
    statusCode
    lang {
      name
      verboseName
    }
    runtimeDisplay
    memoryDisplay
  }
}
"""
