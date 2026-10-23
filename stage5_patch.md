# api/main.py: two lines for stage 5

1. With the other imports:

    from api.stage5 import router as stage5_router

2. Right after `app.add_middleware(...)`:

    app.include_router(stage5_router)
