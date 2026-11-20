"""Reviews: start, dedupe, run cache-first under one shared limiter, emit, resume.

The API depends only on `interfaces.ReviewService`; `build_service` builds the one
implementation, `limiter.build_limiter` the one `SharedLimiter` it and the judge share.
"""
