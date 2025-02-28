## Adaptive Data Pipeline Toolkit - Pipeline module

`python` based, `config-powered` data pipeline for extracting, transforming, and exporting to various destinations. Leverage configuration to automate data journey from extraction to export, tailoring it to specific needs.


### Building and installing the package

`adapt_pipeline` has dependency on following packages. Install them before proceeding further. 
You can find the installation instructions in the respective module `README.md` files.
 * `adapt_connector`
 * `adapt_utils`
 * `adapt_serializer`

Following are the steps to build and install `adapt_pipeline` package.

- clone the repo

  ```shell
  git clone https://github.com/karthick-jaganathan/ADaPT-ETL.git
  ```

- `cd` into the `ADaPT-ETL/pipeline`

  ```shell
  cd ADaPT-ETL/pipeline
  ```

- build and install python package

  ```shell
  python setup.py sdist && pip install dist/adapt_pipeline-0.0.1.tar.gz
  ```

- verifying installation of the package

  ```shell
  pip show adapt_pipeline
  ```

---

### Executing examples

- `cd` into the `ADaPT-ETL`

  ```shell
  cd ADaPT-ETL
  ```

- Execute below command after replacing the placeholder values enclosed in `« »`

  ```shell
  ADAPT_CONFIGS="$(pwd)/configs" adapt_pipeline --namespace facebook \
    --pipeline-config data_ingestion.yaml \
    --data-ingestion-config campaign.yaml \
    --auth-data refresh_token='"«decrypted-access-key»"' \
    --auth-data account_id='"act_«se_acctid»"' \
    --auth-data consumer_key='"«consumer_key»"' \
    --auth-data consumer_secret='"«consumer_secret»"' \
    --external-input campaign_ids='[«comma-separated-campaignid»]'

  ```
---
