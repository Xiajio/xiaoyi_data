from xiaoyi_catalog.normalize import classify_license, derive_modalities, missing_fields


class TestDeriveModalities:
    def test_modality_tags_win_over_task_mapping(self):
        assert derive_modalities(["modality:text", "modality:image"], ["text-classification"]) == (["image", "text"], "tag")

    def test_tabular_tag_normalizes_to_table(self):
        assert derive_modalities(["modality:tabular"], []) == (["table"], "tag")

    def test_task_mapping_covers_text_table_image(self):
        assert derive_modalities([], ["text-classification"]) == (["text"], "task-map")
        assert derive_modalities([], ["question-answering"]) == (["text"], "task-map")
        assert derive_modalities([], ["tabular-regression"]) == (["table"], "task-map")
        assert derive_modalities([], ["image-classification", "object-detection"]) == (["image"], "task-map")

    def test_3d_tasks_map_to_3d(self):
        assert derive_modalities([], ["text-to-3d"]) == (["3d"], "task-map")
        assert derive_modalities([], ["image-to-3d"]) == (["3d"], "task-map")

    def test_empty_or_unmapped_tasks_yield_unknown(self):
        assert derive_modalities([], []) == ([], "unknown")
        assert derive_modalities([], ["some-unlisted-task"]) == ([], "unknown")

    def test_blank_and_malformed_values_are_ignored(self):
        assert derive_modalities(["modality:"], ["  "]) == ([], "unknown")


class TestClassifyLicense:
    def test_permissive(self):
        for value in ["mit", "Apache-2.0", "bsd-3-clause", "cc0-1.0", "cc-by-4.0", "isc"]:
            assert classify_license(value) == "permissive", value

    def test_copyleft(self):
        for value in ["gpl-3.0", "lgpl-2.1", "cc-by-sa-4.0", "mpl-2.0", "agpl-3.0"]:
            assert classify_license(value) == "copyleft", value

    def test_research_only(self):
        for value in ["cc-by-nc-4.0", "cc-by-nc-sa-4.0", "research-only", "non-commercial"]:
            assert classify_license(value) == "research-only", value

    def test_unknown_for_missing_or_unrecognized(self):
        for value in ["", "unknown", "other", "bigscience-openrail-m", "some-custom-license"]:
            assert classify_license(value) == "unknown", value


class TestMissingFields:
    def test_all_missing(self):
        assert missing_fields([], "unknown", "") == ["modalities", "license", "card"]

    def test_nothing_missing(self):
        assert missing_fields(["text"], "permissive", "卡片内容") == []

    def test_partial_missing(self):
        assert missing_fields(["image"], "unknown", "") == ["license", "card"]
