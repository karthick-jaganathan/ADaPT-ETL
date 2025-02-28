## ADaPT (<ins>A</ins>daptive <ins>Da</ins>ta <ins>P</ins>ipeline <ins>T</ins>oolkit)

`python` based, YAML `config-powered` data pipeline for extracting, transforming, and exporting to various destinations. Leverage configuration to automate data journey from extraction to export, tailoring it to specific needs.


### Building and installing the package

`ADaPT` includes following packages. Installing `ADaPT` will install all the packages.
  * `adapt_pipeline`
  * `adapt_connector`
  * `adapt_serializer`
  * `adapt_utils`

Following are the steps to build and install `ADaPT` package.

- clone the repo

  ```shell
  git clone https://github.com/karthick-jaganathan/ADaPT-ETL.git
  ```

- `cd` into the `ADaPT-ETL`

  ```shell
  cd ADaPT-ETL
  ```

- build and install python package

  ```shell
  python setup.py sdist && pip install dist/adapt-0.0.1.tar.gz
  ```

- verifying installation of the package

  ```shell
  pip show adapt
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
