def test_placeholder() -> None:
    import adapters
    import agents
    import api
    import baseline
    import core
    import data
    import evaluation
    import packet

    packages = (
        adapters,
        agents,
        api,
        baseline,
        core,
        data,
        evaluation,
        packet,
    )
    assert all(pkg.__name__ for pkg in packages)
